"""主审与反证分离，错误阶段不得转换为空报告或获得发布资格。"""
import json
from unittest.mock import patch

import pytest

from ai_review import batch_flow, batch_locations, batch_plan, batch_store, batch_worker
from ai_review.common import ReviewError
from test_ai_review_batch_flow import snapshot, config, completed
from test_ai_review_batch_locations import finding, raw
from test_ai_review_batches import state


def plan():
    snap = snapshot('int f(void) { return 0; }\n')
    snap.update(batch_reference_protocol=batch_locations.PROTOCOL,
                batch_evidence_encoding=batch_locations.FILE_LINES_ENCODING,
                batch_output_protocol=batch_locations.PHASE_PROTOCOL)
    return snap, batch_flow.build(snap, config(50000), single_request=True)


@pytest.mark.parametrize('kind', ['primary', 'association'])
def test_review_prompt_only_presents_findings(kind):
    snap, value = plan(); unit = dict(value['units'][0], kind=kind)
    messages = batch_plan.messages(snap, unit)
    system, request = messages[0]['content'], json.loads(messages[1]['content'])
    assert '"findings"' in system and '"checks"' not in system
    assert 'candidates' not in system and '候选反证' not in system
    assert 'candidates' not in request and request['expected_output'] == 'findings'
    parsed = batch_plan.parse_report(raw({'findings': [finding()]}), snap, unit)
    assert len(parsed['findings']) == 1


def test_refutation_prompt_only_presents_checks_and_keeps_all_evidence():
    snap, value = plan(); saved = completed(value)
    saved['units'][0]['result'] = batch_plan.parse_report(raw({'findings': [finding()]}), snap, value['units'][0])
    unit = batch_flow.refutations(snap, saved, config(50000))[0]
    system, request = batch_plan.messages(snap, unit, unit['candidates'])
    assert '"checks"' in system['content'] and '"findings"' not in system['content']
    assert '一次审查完整 PR' not in system['content']
    data = json.loads(request['content'])
    assert data['expected_output'] == 'checks' and len(data['candidates']) == 1
    assert unit['evidence'] == value['units'][0]['evidence']
    check = {'id': unit['candidates'][0]['id'], 'decision': 'confirmed', 'reason': '已核对完整证据及保护'}
    assert len(batch_plan.parse_refutation(raw({'checks': [check]}), unit, unit['candidates'])) == 1


@pytest.mark.parametrize('mixed', [False, True])
def test_checks_cannot_be_accepted_as_primary_even_when_empty(mixed):
    snap, value = plan(); unit = value['units'][0]
    report = {'checks': []}
    if mixed: report['findings'] = []
    with pytest.raises(ReviewError) as error:
        batch_plan.parse_report(raw(report), snap, unit)
    assert error.value.error_code == 'BATCH_OUTPUT_INVALID'
    assert error.value.diagnostic['reason'] == 'source_phase_mismatch'
    assert error.value.diagnostic['expected_output'] == 'findings'
    assert error.value.diagnostic['received_output'] == 'checks'


def test_findings_cannot_be_accepted_as_refutation():
    snap, value = plan(); unit = value['units'][0]
    with pytest.raises(ReviewError) as error:
        batch_plan.parse_refutation(raw({'findings': []}), unit, [])
    assert error.value.diagnostic['expected_output'] == 'checks'


def test_unit_kind_must_agree_with_candidate_presence_before_counting():
    snap, value = plan(); unit = value['units'][0]
    for current, candidates in [(unit, []), (dict(unit, kind='refutation'), None)]:
        with pytest.raises(ReviewError) as error:
            batch_plan.messages(snap, current, candidates)
        assert error.value.error_code == 'BATCH_PHASE_INVALID'


def test_old_saved_unit_retains_mixed_prompt_despite_new_snapshot_marker():
    snap, value = plan(); current = value['units'][0]
    old = dict(current); old.pop('output_protocol')
    system = batch_plan.messages(snap, old)[0]['content']
    assert batch_locations.SYSTEM in system
    assert '"findings"' in system and '"checks"' in system
    assert 'expected_output' not in json.loads(batch_plan.messages(snap, old)[1]['content'])


def test_saved_plan_keeps_phase_marker_and_diagnostic_names_actual_stage(state):
    from ai_review.failure_checks import analyze
    store, job, _ = state; snap, value = plan(); unit = value['units'][0]
    batch_store.save(store, job, value, store.job_config(job['id']))
    with store.tx() as db:
        db.execute('INSERT INTO review_batch_attempts(job,unit,attempt,session_id,created,raw) VALUES(?,?,1,?,0,?)',
                   (job['id'], unit['id'], 'phase-check', json.dumps(raw({'checks': []}))))
    restored = batch_store.get(store, job['id'], private=True)['units'][0]['value']
    assert restored['output_protocol'] == batch_locations.PHASE_PROTOCOL
    report = analyze(store, job, {'error_code': 'BATCH_OUTPUT_INVALID', 'session_id': 'phase-check'})
    check = next(c for c in report['checks'] if c['code'] == 'output_phase')
    assert check['state'] == 'confirmed' and 'findings' in check['detail'] and 'checks' in check['detail']


def test_wrong_phase_never_completes_a_unit_or_starts_position_correction(state):
    store, job, _ = state; snap, value = plan(); cfg = store.job_config(job['id'])
    batch_store.save(store, job, value, cfg)
    record = batch_store.get(store, job['id'], private=True)['units'][0]
    def respond(*args, **kwargs):
        store.update(job['id'], job['owner'], session_status='completed')
        return dict(raw({'checks': []}), _native_result_sha256='a'*64, _native_final_event_seq=1)
    with patch('ai_review.native_review.execute', side_effect=respond) as execute, \
         patch('ai_review.native_review.acknowledge_result'):
        with pytest.raises(ReviewError) as error:
            batch_worker._execute_unit(store, job, cfg, snap, record, 500, value['profile'])
    assert error.value.diagnostic['reason'] == 'source_phase_mismatch'
    saved = batch_store.get(store, job['id'], private=True)
    assert saved['units'][0]['state'] == 'failed' and saved['units'][0]['result'] is None
    assert len(saved['attempts']) == 1 and execute.call_count == 1
