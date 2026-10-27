"""源码行定位协议保留范围校验，不要求模型维护内部证据编号。"""
import copy
import json
from unittest.mock import patch

import pytest

from ai_review import batch_locations, batch_plan, batch_references, batch_flow, batch_store, batch_worker
from ai_review.common import ReviewError
from test_ai_review_batch_flow import snapshot, config
from test_ai_review_batches import state


def unit():
    snap = snapshot('int f(void) { return 0; }\n')
    snap['batch_reference_protocol'] = batch_locations.PROTOCOL
    return snap, batch_flow.build(snap, config())['units'][0]


def raw(value):
    return {'_review_completion': {'complete': True}, 'report_markdown': json.dumps(value)}


def finding(**changes):
    return dict({'path': 'test.c', 'side': 'new', 'line': 1, 'severity': 'P1',
                 'title': '测试问题', 'body': '明确触发与因果链及影响，建议修复'}, **changes)


def test_minimal_report_needs_no_summary_or_internal_ids():
    snap, item = unit()
    parsed = batch_plan.parse_report(raw({'findings': [finding()]}), snap, item)
    result = parsed['findings'][0]
    assert parsed['summary'] == '' and parsed['limitations'] == []
    assert result['body'] == finding()['body']
    assert result['anchor_id'] == 'R1'
    assert result['evidence_ids'] == ['F1H1L1']
    message = json.loads(batch_plan.messages(snap, item)[1]['content'])
    assert all('id' not in r and 'anchor_id' not in r for r in message['evidence'])


@pytest.mark.parametrize('change', [{'path': 'other.c'}, {'side': 'old'}, {'line': 2}, {'line': True}, {'line': 0}, {'body': ''}])
def test_invalid_or_unseen_position_is_rejected(change):
    snap, item = unit()
    with pytest.raises(ReviewError):
        batch_plan.parse_report(raw({'findings': [finding(**change)]}), snap, item)


def test_deleted_code_can_be_commented_on_old_side():
    snap, item = unit()
    row = item['evidence'][0]
    row.update(kind='-', old=51, new=None)
    parsed = batch_plan.parse_report(raw({'findings': [finding(side='old', line=51)]}), snap, item)
    assert parsed['findings'][0]['source_location']['side'] == 'old'


def test_renamed_file_old_location_uses_fixed_base_path():
    snap, item = unit()
    item['evidence'][0].update(path='after.c', old_path='before.c', new_path='after.c', kind='-', old=51, new=None)
    parsed = batch_plan.parse_report(raw({'findings': [finding(path='before.c', side='old', line=51)]}), snap, item)
    assert parsed['findings'][0]['anchor_id'] == 'R1'
    rows, _ = batch_locations.payload(item)
    assert rows[0]['path'] == 'before.c'
    with pytest.raises(ReviewError):
        batch_plan.parse_report(raw({'findings': [finding(path='after.c', side='old', line=51)]}), snap, item)


def test_context_is_related_evidence_but_not_inline_anchor():
    snap, item = unit()
    item['evidence'].append(dict(item['evidence'][0], id='上下文', kind='context', new=2, anchor_id=None))
    value = {'findings': [finding(related_locations=[{'path': 'test.c', 'side': 'new', 'line': 2}])]}
    assert len(batch_plan.parse_report(raw(value), snap, item)['findings'][0]['evidence_ids']) == 2
    with pytest.raises(ReviewError):
        batch_plan.parse_report(raw({'findings': [finding(line=2)]}), snap, item)


def test_ambiguous_position_never_chooses_closest_or_first():
    snap, item = unit()
    item['evidence'].append(dict(item['evidence'][0], id='另一证据'))
    with pytest.raises(ReviewError) as error:
        batch_plan.parse_report(raw({'findings': [finding()]}), snap, item)
    assert error.value.diagnostic['reason'] == 'source_location_ambiguous'


def test_related_locations_must_be_in_current_unit():
    snap, item = unit()
    with pytest.raises(ReviewError):
        batch_plan.parse_report(raw({'findings': [finding(related_locations=[{'path': 'test.c', 'side': 'new', 'line': 10}])]}), snap, item)


def test_refutation_covers_every_candidate_without_copying_evidence_ids():
    snap, item = unit()
    candidates = batch_plan.parse_report(raw({'findings': [finding()]}), snap, item)['findings']
    item['kind'] = 'refutation'
    item['candidates'] = candidates
    result = batch_plan.parse_refutation(raw({'checks': [{'id': candidates[0]['id'], 'decision': 'confirmed', 'reason': '已核对提供的代码及保护'}]}), item, candidates)
    assert result[0]['evidence_ids'] == candidates[0]['evidence_ids']
    with pytest.raises(ReviewError):
        batch_plan.parse_refutation(raw({'checks': []}), item, candidates)


def test_location_correction_can_only_change_positions():
    before = {'findings': [finding(line=2)]}
    after = {'findings': [finding(line=1)]}
    assert batch_references.unchanged_report(before, after, locations=True)
    changed = copy.deepcopy(after)
    changed['findings'][0]['body'] = '改写意见'
    assert not batch_references.unchanged_report(before, changed, locations=True)
    assert not batch_references.unchanged_report(before, {'findings': []}, locations=True)


def test_wrong_source_line_gets_one_reference_only_correction(state):
    store, job, _ = state
    snap = snapshot('int f(void) { return 0; }\n')
    snap['batch_reference_protocol'] = batch_locations.PROTOCOL
    cfg = store.job_config(job['id'])
    value = batch_flow.build(snap, cfg)
    batch_store.save(store, job, value, cfg)
    calls = []
    def respond(*args, **kwargs):
        calls.append(kwargs['diff_snapshot']['_batch_messages'])
        store.update(job['id'], job['owner'], session_status='completed')
        return dict(raw({'findings': [finding(line=2 if len(calls) == 1 else 1)]}),
                    _native_result_sha256='a' * 64, _native_final_event_seq=1)
    with patch('ai_review.native_review.execute', side_effect=respond), patch('ai_review.native_review.acknowledge_result'):
        record = batch_store.get(store, job['id'], private=True)['units'][0]
        assert batch_worker._execute_unit(store, job, cfg, snap, record, 500, value['profile']) is False
        record = batch_store.get(store, job['id'], private=True)['units'][0]
        assert batch_worker._execute_unit(store, job, cfg, snap, record, 500, value['profile']) is True
    assert len(calls) == 2
    assert 'previous_report' in json.loads(calls[1][1]['content'])
    assert len(batch_store.get(store, job['id'])['units']) == 1


def test_natural_body_survives_grouped_refutation_and_publication_adapter():
    from ai_review import diff_inline
    from test_ai_review_batch_flow import completed
    snap, first = unit()
    cfg = config()
    value = batch_flow.build(snap, cfg)
    saved = completed(value)
    saved['units'][0]['result'] = batch_plan.parse_report(raw({'findings': [finding()]}), snap, first)
    groups = batch_flow.refutations(snap, saved, cfg)
    group = groups[0]
    result = batch_plan.parse_refutation(raw({'checks': [{'id': group['candidates'][0]['id'], 'decision': 'confirmed', 'reason': '已核对保护条件'}]}), group, group['candidates'])
    saved['units'].append(dict(id=group['id'], kind='refutation', state='done', value=group, result=result))
    snap['anchors'], _ = diff_inline.index(snap)
    report = batch_worker.aggregate(snap, saved)
    assert finding()['body'] in report['findings'][0]['comment_markdown']
    assert report['report_markdown'].startswith('VERDICT: FAIL')


def test_readonly_failure_check_uses_position_schema(state):
    from ai_review.failure_checks import analyze
    store, job, _ = state
    snap, item = unit()
    cfg = store.job_config(job['id'])
    value = batch_flow.build(snap, cfg)
    batch_store.save(store, job, value, cfg)
    item = value['units'][0]
    answer = raw({'findings': [finding(line=999)]})
    with store.tx() as db:
        db.execute('INSERT INTO review_batch_attempts(job,unit,attempt,session_id,created,raw) VALUES(?,?,1,?,0,?)',
                   (job['id'], item['id'], 'position-check', json.dumps(answer)))
    result = analyze(store, job, {'error_code': 'BATCH_OUTPUT_INVALID', 'session_id': 'position-check'})
    check = next(c for c in result['checks'] if c['code'] == 'source_locations')
    assert check['state'] == 'confirmed' and '不在本单元' in check['detail']
    assert not any(c['code'] == 'finding_fields' for c in result['checks'])
