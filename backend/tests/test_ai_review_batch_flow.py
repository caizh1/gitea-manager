"""语法关系、分组反证、预算和旧计划边界；真实模型质量另行验收。"""
import copy
import difflib
import json
from unittest.mock import patch

import pytest

from ai_review import batch_flow, batch_graph, batch_plan, batch_store, batch_worker, token_budget
from ai_review.common import ReviewError
from test_ai_review_batches import state


def snapshot(text):
    return {'id': '固定快照', 'number': 1, 'title': '测试修改', 'description': '',
            'head': 'a' * 40, 'base': 'b' * 40, 'merge_base': 'b' * 40,
            'diff': 'diff --git a/test.c b/test.c\n' + ''.join(difflib.unified_diff([], text.splitlines(True), fromfile='/dev/null', tofile='b/test.c')),
            'analysis_sources': [{'path': 'test.c', 'source_path': 'test.c', 'side': 'new',
                                  'version': 'a' * 40, 'sha256': '源码摘要', 'text': text}],
            'anchors': {}, 'coverage': {'scope': 'diff', 'complete': True}}


def config(limit=6000):
    return {'model': '测试模型', 'batch_input_tokens': limit, 'model_context_tokens': 65536,
            'model_max_output_tokens': 8192}


def plan(text, limit=6000):
    return batch_flow.build(snapshot(text), config(limit))


def example_candidate(unit, title='同名问题', severity='P1'):
    row = next(r for r in unit['evidence'] if r['anchor_id'])
    return {'id': unit['id'] + ':1', 'severity': severity, 'title': title,
            'trigger': '明确触发', 'explanation': '证据因果链', 'impact': '严重影响', 'recommendation': '修复建议',
            'evidence_ids': [row['id']], 'anchor_id': row['anchor_id']}


def completed(value):
    return {'hash': value['hash'], 'value': value,
            'units': [dict(id=u['id'], kind=u['kind'], value=u, state='done',
                           result={'summary': '检查完成', 'findings': [], 'limitations': []}) for u in value['units']]}


def test_common_types_do_not_generate_pairwise_associations():
    value = plan(''.join(f'uint32_t task_{i}(uint32_t x) {{ return x + {i}; }}\n' for i in range(80)))
    assert value['primary'] > 1
    assert value['association'] == 0
    assert not value['relations']
    core = [i for u in value['units'] for i in u['core']]
    assert len(core) == len(set(core)) == 80


@pytest.mark.parametrize('text,kind', [
    ('int target(int x) { return x; }\nint caller(void) { return target(9); }\n', 'call'),
    ('struct Device { int ready; };\nvoid set(struct Device *d) { d->ready = 1; }\nint get(struct Device *d) { return d->ready; }\n', 'state'),
    ('struct Device { char *buffer; };\nvoid init(struct Device *d) { d->buffer = malloc(9); }\nvoid close(struct Device *d) { free(d->buffer); }\n', 'resource'),
])
def test_real_relations_are_recorded_even_without_candidates(text, kind):
    value = plan(text)
    assert any(r['kind'] == kind for r in value['relations'])
    assert all(r['reason'] and len(r['code']) == 2 for r in value['relations'])


def test_cross_batch_call_is_not_skipped_when_primary_has_no_findings():
    text = 'int target(int x) {\n' + ' x += 1;\n' * 10 + ' return x;\n}\n'
    text += 'int unrelated(int x) {\n' + ' x += 2;\n' * 35 + ' return x;\n}\n'
    text += 'int caller(void) {\n' + ' int x = 0;\n' + ' x += 1;\n' * 10 + ' return target(x);\n}\n'
    value = plan(text, 12500)
    assert value['primary'] >= 2
    assert value['association'] >= 1
    assert any(c['kind'] == 'call' for u in value['units'] if u['kind'] == 'association' for c in u['connections'])


def test_identical_titles_with_different_evidence_are_not_merged():
    value = plan('int f(void) { return 1; }\nint g(void) { return 2; }\n')
    saved = completed(value)
    unit = saved['units'][0]
    a = example_candidate(unit['value'])
    b = dict(a, id=a['id'] + 'b', explanation='不同根因')
    unit['result']['findings'] = [a, b, dict(a, id=a['id'] + 'copy')]
    candidates = batch_flow.candidates(saved)
    assert len(candidates) == 2
    assert len(candidates[0]['origins']) == 2


def test_grouped_refutation_preserves_candidates_and_protection():
    text = 'int f(int x) { if (x < 0) return 0; return x + 1; }\n'
    snap, cfg = snapshot(text), config()
    value = batch_flow.build(snap, cfg)
    saved = completed(value)
    first = saved['units'][0]
    one = example_candidate(first['value'])
    two = dict(one, id='第二候选', explanation='第二条不同因果链')
    first['result']['findings'] = [one, two, dict(one, id='非阻塞', severity='P2')]
    groups = batch_flow.refutations(snap, saved, cfg)
    assert len(groups) == 1 and len(groups[0]['candidates']) == 2
    assert 'if (x < 0)' in ''.join(r['text'] for r in groups[0]['evidence'])
    raw = {'_review_completion': {'complete': True}, 'report_markdown': '{"checks": []}'}
    with pytest.raises(ReviewError):
        batch_plan.parse_refutation(raw, groups[0], groups[0]['candidates'])


def test_grouped_refutation_splits_at_actual_request_budget():
    snap = snapshot('int f(void) { return 0; }\n')
    cfg = config(6000)
    value = batch_flow.build(snap, cfg)
    saved = completed(value)
    sample = example_candidate(saved['units'][0]['value'])
    saved['units'][0]['result']['findings'] = [dict(sample, id=str(i), explanation=str(i) + '证据' * 160) for i in range(8)]
    groups = batch_flow.refutations(snap, saved, cfg)
    assert len(groups) > 1
    assert sum(len(u['candidates']) for u in groups) == 8
    assert all(batch_flow.count(snap, u, cfg, value['profile']) <= token_budget.capacity(cfg) for u in groups)


def test_refutation_install_is_idempotent_and_keeps_approved_bound(state):
    store, job, _ = state
    snap, cfg = snapshot('int f(void) { return 1; }\n'), store.job_config(job['id'])
    value = batch_flow.build(snap, cfg)
    batch_store.save(store, job, value, cfg)
    current = batch_store.get(store, job['id'], private=True)
    unit = batch_flow.make_unit(snap, 'refutation', value['units'][0]['evidence'], candidates=[])
    batch_store.save_refutations(store, job, current, [unit])
    batch_store.save_refutations(store, job, current, [unit])
    actual = batch_store.get(store, job['id'], private=True)
    assert sum(u['kind'] == 'refutation' for u in actual['units']) == 1
    assert actual['value']['refutation_planned'] is True
    assert 'scopes' not in batch_store.get(store, job['id'])['value']


def test_v3_rejects_foreign_evidence_and_incomplete_checks():
    unit = plan('int f(void) { return 0; }\n')['units'][0]
    candidate = example_candidate(unit)
    candidate['evidence_ids'] = ['E0000000000']
    raw = {'_review_completion': {'complete': True}, 'report_markdown': json.dumps(
        {'summary': '', 'limitations': [], 'findings': [candidate]})}
    with pytest.raises(ReviewError):
        batch_plan.parse_report(raw, {}, unit)


def test_legacy_builder_keeps_legacy_version():
    value = batch_plan.build(snapshot('int f(void) { return 0; }\n'), config(32768))
    assert value['version'] == 'diff-batches-v2'
    assert all('flow_version' not in u for u in value['units'])


def test_unresolved_function_pointer_is_explicit_limitation():
    value = plan('int run(int (*callback)(void)) { return callback(); }\n')
    assert value['limitations']


def test_nested_state_members_do_not_create_container_wide_relations():
    value = plan('struct Inner { int a, b; }; struct Device { struct Inner ext; };\n'
                 'void first(struct Device *d) { d->ext.a = 1; }\n'
                 'int second(struct Device *d) { return d->ext.b; }\n')
    assert not [r for r in value['relations'] if r['kind'] == 'state']


def test_changed_interface_and_indexed_shared_state_are_connected():
    value = plan('#define CAPACITY 4\nstruct Device { int items[CAPACITY]; };\n'
                 'void put(struct Device *d, int i) { d->items[i] = 1; }\n'
                 'int get(struct Device *d, int i) { return d->items[i]; }\n')
    assert any(r['kind'] == 'interface' for r in value['relations'])
    assert any(r['kind'] == 'state' for r in value['relations'])


def test_bulk_reset_is_related_to_nested_state_reader():
    value = plan('struct Inner { int a; }; struct Device { struct Inner ext; };\n'
                 'void reset(struct Device *d) { memset(&d->ext, 0, sizeof(d->ext)); }\n'
                 'int read_state(struct Device *d) { return d->ext.a; }\n')
    assert any(r['kind'] == 'state' and r['detail'].get('whole_object_write') for r in value['relations'])


def test_context_only_functions_do_not_create_unrelated_cross_checks():
    old = 'int state;\nvoid set(void) { state = 1; }\nint get(void) { return state; }\n'
    new = old + 'int unrelated(void) { return 5; }\n'
    snap = snapshot(new)
    snap['diff'] = 'diff --git a/test.c b/test.c\n' + ''.join(difflib.unified_diff(
        old.splitlines(True), new.splitlines(True), fromfile='a/test.c', tofile='b/test.c'))
    snap['analysis_sources'].append(dict(snap['analysis_sources'][0], side='old', version='b' * 40, text=old))
    value = batch_flow.build(snap, config())
    assert value['changed_lines'] == 1
    assert not value['relations']


def test_every_function_keeps_file_contract_and_its_leading_comment():
    value = plan('/* 控制寄存器置位会启动真实硬件，失败时不得改变状态。 */\n'
                 'int first(void) { return 0; }\n'
                 '/* 临界区退出后必须恢复关键故障中断。 */\n'
                 'int restore(void) { return 1; }\n')
    scope = next(s for s in value['scopes'].values() if s['name'] == 'restore')
    text = ''.join(r['text'] for r in scope['evidence'])
    assert '置位会启动真实硬件' in text
    assert '必须恢复关键故障中断' in text


def test_change_impact_reaches_unchanged_callers_and_state_users():
    old = ('int shared;\nint helper(void) { return 1; }\n'
           'void caller(void) { shared = helper(); }\n'
           'int observer(void) { return shared; }\n')
    new = old.replace('return 1;', 'return 0;')
    snap = snapshot(new)
    snap['diff'] = 'diff --git a/test.c b/test.c\n' + ''.join(difflib.unified_diff(
        old.splitlines(True), new.splitlines(True), fromfile='a/test.c', tofile='b/test.c'))
    snap['analysis_sources'].append(dict(snap['analysis_sources'][0], side='old', version='b' * 40, text=old))
    value = batch_flow.build(snap, config())
    assert any(r['kind'] == 'state' and {c['name'] for c in r['code']} == {'caller', 'observer'}
               for r in value['relations'])


def test_refutation_preserves_supplied_outer_caller_protection():
    text = ('int leaf(int n) { return 16 / n; }\n'
            'int middle(int n) { return leaf(n); }\n'
            'int outer(int n) { if (n == 0) return -1; return middle(n); }\n')
    snap, cfg = snapshot(text), config(16000)
    value = batch_flow.build(snap, cfg)
    saved = completed(value)
    record = saved['units'][0]
    candidate = example_candidate(record['value'])
    candidate['evidence_ids'] = [next(r['id'] for r in record['value']['evidence'] if '16 / n' in r['text'])]
    record['result']['findings'] = [candidate]
    groups = batch_flow.refutations(snap, saved, cfg)
    assert 'if (n == 0)' in ''.join(r['text'] for r in groups[0]['evidence'])


def test_schema_17_upgrade_preserves_old_flow(state):
    from ai_review.store import Store
    store, job, _ = state
    with store.tx() as db:
        db.execute('ALTER TABLE jobs DROP COLUMN batch_flow_version')
        db.execute('PRAGMA user_version=17')
    reopened = Store(store.path)
    assert reopened.job(job['id'])['batch_flow_version'] == 2
    assert reopened.job(job['id'])['owner'] == job['owner']
    with reopened.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 18


def test_v3_vendor_window_failure_does_not_apply_legacy_midpoint_rewrite(state):
    store, job, _ = state
    snap, cfg = snapshot('int f(void) { return 0; }\n'), store.job_config(job['id'])
    value = batch_flow.build(snap, cfg)
    batch_store.save(store, job, value, cfg)
    record = batch_store.get(store, job['id'], private=True)['units'][0]
    assert not batch_store.adjust_context(store, job, record, {'upstream_error_code': 'context_length_exceeded'}, cfg, snap)
    assert batch_store.get(store, job['id'])['hash'] == value['hash']


def test_same_unit_retains_protection_constants_from_fixed_source():
    text = '#define LIMIT 4\nint f(int n) { if (n >= LIMIT) return -1; return n; }\n'
    value = plan(text)
    scope = next(iter(value['scopes'].values()))
    assert any('#define LIMIT 4' in r['text'] for r in scope['evidence'])


def test_cross_only_finding_reaches_independent_refutation_and_verdict():
    text = 'int divide(int x) { return 16 / x; }\n'
    text += 'int unrelated(int x) {\n' + ' x += 2;\n' * 35 + ' return x;\n}\n'
    text += 'int caller(void) { return divide(0); }\n'
    snap, cfg = snapshot(text), config(7000)
    value = batch_flow.build(snap, cfg)
    saved = completed(value)
    association = next(u for u in saved['units'] if u['kind'] == 'association')
    candidate = example_candidate(association['value'])
    association['result']['findings'] = [candidate]
    assert all(not u['result']['findings'] for u in saved['units'] if u['kind'] == 'primary')
    groups = batch_flow.refutations(snap, saved, cfg)
    assert len(groups) == 1
    from ai_review.batch_references import payload
    from ai_review import diff_inline
    unit = groups[0]
    _, local_candidates = payload(unit, unit['candidates'])
    check = dict(id=local_candidates[0]['id'], decision='confirmed', reason='调用方传零且没有保护',
                 evidence_ids=local_candidates[0]['evidence_ids'])
    result = batch_plan.parse_refutation({'_review_completion': {'complete': True},
        'report_markdown': json.dumps({'checks': [check]})}, unit, unit['candidates'])
    saved['units'].append(dict(id=unit['id'], kind='refutation', state='done', value=unit, result=result))
    snap['anchors'], _ = diff_inline.index(snap)
    report = batch_worker.aggregate(snap, saved)
    assert report['report_markdown'].startswith('VERDICT: FAIL')
    assert len(report['findings']) == 1
    assert candidate['id'] in report['findings'][0]['origins']


def test_reference_receipt_loss_does_not_repeat_v3_model_call(state):
    from ai_review.batch_references import payload
    store, job, _ = state
    snap, cfg = snapshot('int f(void) { return 0; }\n'), store.job_config(job['id'])
    value = batch_flow.build(snap, cfg)
    batch_store.save(store, job, value, cfg)
    record = batch_store.get(store, job['id'], private=True)['units'][0]
    raw = {'_review_completion': {'complete': True}, '_native_result_sha256': 'a' * 64,
           '_native_final_event_seq': 1, 'report_markdown': json.dumps({'summary': '', 'findings': [], 'limitations': []})}
    def respond(*args, **kwargs):
        store.update(job['id'], job['owner'], session_status='completed')
        return raw
    error = ReviewError('回执未知')
    error.error_code = 'EXECUTOR_RESULT_ACK_UNKNOWN'
    with patch('ai_review.native_review.execute', side_effect=respond) as model, \
         patch('ai_review.native_review.acknowledge_result', side_effect=[error, None]):
        with pytest.raises(ReviewError):
            batch_worker._execute_unit(store, job, cfg, snap, record, 500, value['profile'])
        record = batch_store.get(store, job['id'], private=True)['units'][0]
        assert record['state'] == 'unknown'
        assert batch_worker._execute_unit(store, job, cfg, snap, record, 500, value['profile'])
        assert model.call_count == 1
    assert len(batch_store.get(store, job['id'])['units']) == 1


def test_aggregate_refuses_missing_refutation():
    snap = snapshot('int f(void) { return 0; }\n')
    value = batch_flow.build(snap, config())
    saved = completed(value)
    saved['units'][0]['result']['findings'] = [example_candidate(saved['units'][0]['value'])]
    with pytest.raises(ReviewError, match='遗漏'):
        batch_worker.aggregate(snap, saved)


def test_cancelled_owner_cannot_install_groups(state):
    store, job, _ = state
    snap, cfg = snapshot('int f(void) { return 0; }\n'), store.job_config(job['id'])
    value = batch_flow.build(snap, cfg)
    batch_store.save(store, job, value, cfg)
    current = batch_store.get(store, job['id'], private=True)
    with store.tx() as db:
        db.execute("UPDATE jobs SET status='cancelled',owner='' WHERE id=?", (job['id'],))
    with pytest.raises(ReviewError):
        batch_store.save_refutations(store, job, current, [])


def test_new_jobs_are_versioned_and_existing_rows_not_upgraded(state):
    store, job, repo = state
    created = store.enqueue(repo['id'], 99, {'head': 'c' * 40, 'base': 'b' * 40})
    assert store.job(created)['batch_flow_version'] == 5
    with store.tx() as db:
        db.execute('UPDATE jobs SET batch_flow_version=2 WHERE id=?', (job['id'],))
        batch_store.migrate(db)
    assert store.job(job['id'])['batch_flow_version'] == 2


def test_two_hundred_lines_fit_one_primary_without_redundant_association():
    header = ['typedef struct { int counter; } State;'] + ['/* 文件级接口约束 */'] * 39
    first = ['int first(State *state)', '{'] + ['    state->counter += 1;'] * 76 + ['    return state->counter;', '}']
    second = ['int second(State *state)', '{', '    int result = first(state);'] + ['    state->counter += 1;'] * 75 + ['    return result;', '}']
    text = '\n'.join(header + first + second) + '\n'
    assert len(text.splitlines()) == 200
    value = plan(text, 65536)
    assert value['primary'] == 1 and value['association'] == 0
    assert value['changed_lines'] == 200 and not value['gaps']
    assert len(value['units'][0]['core']) == 200
    assert any(r['kind'] == 'call' and r.get('covered_by') for r in value['relations'])
