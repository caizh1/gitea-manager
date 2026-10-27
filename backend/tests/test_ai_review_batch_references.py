"""本批编号隔离、严格解析及一次纠正；固定响应不冒充真实模型验收。"""
import copy
import json
from unittest.mock import patch

import pytest

from ai_review import batch_plan, batch_references, batch_store, batch_worker
from ai_review.common import ReviewError
from test_ai_review_batches import state, snapshot
from test_ai_review_diff import Client


def sample():
    unit = {'id': '本批次', 'kind': 'primary', 'reference_protocol': batch_references.PROTOCOL,
            'evidence': [{'id': f'F4H1L{i}', 'anchor_id': f'R{i+113}', 'kind': '+',
                          'text': '设备状态更新\n', 'new': i, 'old': None} for i in range(95, 249)]}
    return unit


def raw(finding):
    return {'report_markdown': json.dumps({'summary': '检查完成', 'findings': [finding], 'limitations': []}),
            '_review_completion': {'complete': True}, '_native_result_sha256': 'a'*64, '_native_final_event_seq': 1}


def finding(ident, anchor):
    return dict(severity='P1', title='设备状态错误', trigger='运行中请求', explanation='缺少必要保护',
                impact='设备状态损坏', recommendation='补齐保护', evidence_ids=[ident], anchor_id=anchor)


def test_observed_154_rows_expose_one_namespace_and_resolve_exactly():
    unit = sample(); before = copy.deepcopy(unit)
    rows, _ = batch_references.payload(unit)
    assert len({r['id'] for r in rows}) == 154
    assert all(r['id'] == r['anchor_id'] for r in rows)
    assert all(r['id'].startswith('E') for r in rows)
    result = batch_plan.parse_report(raw(finding(rows[0]['id'], rows[0]['id'])), {}, unit)
    assert result['findings'][0]['evidence_ids'] == ['F4H1L95']
    assert result['findings'][0]['anchor_id'] == 'R208'
    assert unit == before


@pytest.mark.parametrize('reference', ['F4H1L308', 'F4H1L95', 'R208', 'R320', 'E0000000000', 'Eed719850711', 'E1-E3'])
def test_new_protocol_rejects_guessed_legacy_and_other_batch_ids(reference):
    unit = sample()
    with pytest.raises(ReviewError):
        batch_plan.parse_report(raw(finding(reference, None)), {}, unit)


def test_tokens_are_bound_to_unit_membership_not_global_row_numbers():
    first = sample(); second = copy.deepcopy(first); second['evidence'] = second['evidence'][1:]
    key = next(iter(batch_references.mapping(first)))
    with pytest.raises(ReviewError):
        batch_plan.parse_report(raw(finding(key, None)), {}, second)
    keys = list(batch_references.mapping(first))
    with pytest.raises(ReviewError) as error:
        batch_plan.parse_report(raw(finding(keys[0], keys[1])), {}, first)
    assert error.value.diagnostic['reason'] == 'batch_anchor_mismatch'


def test_refutation_converts_candidates_without_mutating_canonical_record():
    unit = sample(); candidate = finding('F4H1L95', 'R208'); candidate['id'] = '候选一'
    before = copy.deepcopy(candidate)
    rows, candidates = batch_references.payload(unit, [candidate])
    assert candidates[0]['evidence_ids'] == [rows[0]['id']]
    answer = {'checks': [dict(id='候选一', decision='confirmed', reason='已核对', evidence_ids=[rows[0]['id']])]}
    result = batch_plan.parse_refutation(dict(report_markdown=json.dumps(answer), _review_completion={'complete': True}), unit, [candidate])
    assert result[0]['evidence_ids'] == ['F4H1L95']
    assert candidate == before


def test_correction_uses_task_schema_when_report_has_extra_checks_field():
    before = {'summary': '相同', 'findings': [finding('错误编号', None)], 'limitations': [], 'checks': []}
    after = copy.deepcopy(before)
    after['findings'][0]['evidence_ids'] = ['合法编号']
    assert batch_references.unchanged_report(before, after, field='findings')
    assert batch_references.unchanged_report(before, after)
    after['checks'] = [{'新增': '不得改变额外字段'}]
    assert not batch_references.unchanged_report(before, after, field='findings')
    after = copy.deepcopy(before)
    after['findings'][0]['severity'] = 'P2'
    assert not batch_references.unchanged_report(before, after, field='findings')


@pytest.mark.parametrize('second_valid', [True, False])
@pytest.mark.parametrize('invalid_ref', ['F4H1L308', 'Eed719850711'])
def test_complete_bad_reference_gets_only_one_fresh_attempt_and_retains_first(state, second_valid, invalid_ref):
    store, job, _ = state
    cfg = store.job_config(job['id']); snap = snapshot(cfg); plan = batch_plan.build(snap, cfg)
    batch_store.save(store, job, plan, cfg)
    assert plan['max_calls'] == 4 * (plan['primary'] + plan['association']) + plan['history']
    calls = []
    def respond(_store, current, _config, workspace, token, diff_snapshot):
        calls.append((current['session_id'], diff_snapshot['_batch_messages']))
        store.update(job['id'], job['owner'], session_status='completed')
        payload = json.loads(diff_snapshot['_batch_messages'][1]['content'])
        row = next(r for r in payload['evidence'] if r['anchor_id'])
        ident = row['id'] if len(calls) == 2 and second_valid else invalid_ref
        return raw(finding(ident, row['anchor_id']))
    record = batch_store.get(store, job['id'], private=True)['units'][0]
    with patch('ai_review.native_review.execute', side_effect=respond), patch('ai_review.native_review.acknowledge_result'):
        assert batch_worker._execute_unit(store, job, cfg, snap, record, 500, plan['profile']) is False
        record = batch_store.get(store, job['id'], private=True)['units'][0]
        if second_valid:
            assert batch_worker._execute_unit(store, job, cfg, snap, record, 500, plan['profile']) is True
        else:
            with pytest.raises(ReviewError):
                batch_worker._execute_unit(store, job, cfg, snap, record, 500, plan['profile'])
    assert len(calls) == 2 and calls[0][0] != calls[1][0]
    assert '上次完整响应' in calls[1][1][0]['content']
    with store.connect() as db:
        rows = db.execute('SELECT state,raw,session FROM review_batch_attempts WHERE job=? ORDER BY attempt', (job['id'],)).fetchall()
    assert rows[0]['state'] == 'failed' and invalid_ref in rows[0]['raw']
    assert json.loads(rows[0]['session'])['reference_validation']['reason'] == 'batch_evidence_unknown'
    assert rows[1]['state'] == ('done' if second_valid else 'failed')


@pytest.mark.parametrize('failure', ['incomplete', 'json', 'ack', 'unknown'])
def test_other_failures_never_schedule_automatic_regeneration(state, failure):
    store, job, _ = state
    cfg = store.job_config(job['id']); snap = snapshot(cfg); plan = batch_plan.build(snap, cfg)
    batch_store.save(store, job, plan, cfg)
    record = batch_store.get(store, job['id'], private=True)['units'][0]
    answer = raw(finding('F4H1L308', None))
    if failure == 'incomplete': answer['_review_completion']['complete'] = False
    if failure == 'json': answer['report_markdown'] = '不是 JSON'
    def respond(*args, **kwargs):
        if failure != 'unknown': store.update(job['id'], job['owner'], session_status='completed')
        return answer
    ack = ReviewError('回执未知') if failure == 'ack' else None
    if ack: ack.error_code = 'EXECUTOR_RESULT_ACK_UNKNOWN'
    with patch('ai_review.native_review.execute', side_effect=respond) as call, \
         patch('ai_review.native_review.acknowledge_result', side_effect=ack):
        with pytest.raises(ReviewError):
            batch_worker._execute_unit(store, job, cfg, snap, record, 500, plan['profile'])
    assert call.call_count == 1
    saved = batch_store.get(store, job['id'], private=True)
    assert saved['units'][0]['state'] in ('failed', 'unknown')
    assert len(saved['attempts']) == 1


def test_legacy_units_keep_original_contract_and_never_guess_new_ids():
    unit = sample(); unit.pop('reference_protocol')
    result = batch_plan.parse_report(raw(finding('R208', 'R208')), {}, unit)
    assert result['findings'][0]['evidence_ids'] == ['F4H1L95']
    with pytest.raises(ReviewError):
        batch_plan.parse_report(raw(finding('F4H1L308', 'R320')), {}, unit)


def test_source_metadata_is_preserved_once_and_not_repeated_in_every_row():
    unit = sample()
    for row in unit['evidence']:
        row.update(path='after.c', old_path='before.c', new_path='after.c')
    snap = dict(id='固定快照', number=1, title='标题', description='说明', head='a', base='b', merge_base='b')
    body = json.loads(batch_plan.messages(snap, unit)[1]['content'])
    assert body['files'] == [dict(path='after.c', old_path='before.c', new_path='after.c')]
    assert all('old_path' not in row and 'new_path' not in row for row in body['evidence'])
    assert [row['text'] for row in body['evidence']] == [row['text'] for row in unit['evidence']]


@pytest.mark.parametrize('change', ['drop', 'severity', 'explanation', 'order'])
def test_correction_cannot_hide_downgrade_or_rewrite_findings(change):
    original = json.loads(raw(finding('F4H1L308', 'R320'))['report_markdown'])
    other = finding('F4H1L310', 'R321'); other['title'] = '另一个问题'
    original['findings'].append(other)
    corrected = copy.deepcopy(original)
    if change == 'drop': corrected['findings'] = []
    elif change == 'severity': corrected['findings'][0]['severity'] = 'P3'
    elif change == 'explanation': corrected['findings'][0]['explanation'] = '改写因果链'
    elif change == 'order': corrected['findings'].reverse()
    assert not batch_references.unchanged_report(original, corrected)
    fixed = copy.deepcopy(original)
    fixed['findings'][0].update(evidence_ids=['E123456abcd'], anchor_id='E123456abcd')
    assert batch_references.unchanged_report(original, fixed)
