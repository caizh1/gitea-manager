"""无损证据编码：保护完整输入、旧任务契约与严格源码位置校验。"""
import copy
import json
from unittest.mock import patch

import pytest

from ai_review import batch_flow, batch_locations, batch_plan, batch_worker, token_budget
from ai_review.common import ReviewError
from test_ai_review_batch_flow import snapshot, config, completed
from test_ai_review_batch_locations import finding, raw
from test_ai_review_batches import state
from test_ai_review_diff import Client


def decode(encoded):
    return [dict({k: v for k, v in group.items() if k != 'lines'},
                 **dict(zip(encoded['columns'], line)))
            for group in encoded['files'] for line in group['lines']]


def test_file_lines_round_trip_keeps_versions_rename_sides_and_order():
    rows = [dict(path='new.c', old_path='old.c', version='base', kind='-', old=12, new=None,
                 anchor_available=True, text='旧代码'),
            dict(path='new.c', old_path='old.c', version='head', kind='+', old=None, new=13,
                 anchor_available=True, text='新代码'),
            dict(path='new.c', old_path='old.c', version='head', kind='context', old=None, new=14,
                 anchor_available=False, text='现有保护'),
            dict(path='other.c', version='base', kind=' ', old=5, new=5,
                 anchor_available=False, text='共享声明')]
    rows += [copy.deepcopy(rows[0])]
    encoded = batch_locations.file_lines(rows)
    assert decode(encoded) == rows
    assert len(encoded['files']) == 4
    assert batch_locations.file_lines([])['files'] == []


def plan(compact):
    snap = snapshot('int f(void) { return 0; }\n')
    snap['batch_reference_protocol'] = batch_locations.PROTOCOL
    if compact:
        snap['batch_evidence_encoding'] = batch_locations.FILE_LINES_ENCODING
    value = batch_flow.build(snap, config(50000), single_request=True)
    return snap, value


def test_new_encoding_keeps_all_evidence_and_old_messages_unchanged():
    snap, legacy = plan(False)
    original = copy.deepcopy(legacy['units'][0])
    old_messages = batch_plan.messages(snap, original)
    compact = dict(original, evidence_encoding=batch_locations.FILE_LINES_ENCODING)
    current = batch_plan.messages(snap, compact)
    old_data, new_data = [json.loads(messages[1]['content']) for messages in (old_messages, current)]
    assert decode(new_data.pop('evidence')) == old_data.pop('evidence')
    assert new_data == old_data
    assert current[0]['content'].replace(batch_locations.FILE_LINES_SYSTEM, '') == old_messages[0]['content']
    assert batch_plan.messages(snap, original) == old_messages
    assert compact['evidence'] == original['evidence']
    changed_snapshot = dict(snap, batch_evidence_encoding=batch_locations.FILE_LINES_ENCODING)
    assert batch_plan.messages(changed_snapshot, original) == old_messages


@pytest.mark.parametrize('version,encoding', [(6, None), (7, batch_locations.FILE_LINES_ENCODING), (8, batch_locations.FILE_LINES_ENCODING)])
def test_worker_freezes_encoding_only_for_new_job_versions(state, version, encoding):
    store, job, _ = state
    job['batch_flow_version'] = version
    snap = snapshot('int f(void) { return 0; }\n')
    with patch('ai_review.worker.client_for', return_value=Client()), \
         patch('ai_review.diff_review.capture', return_value=snap), \
         patch('ai_review.batch_worker.assert_version'), \
         patch('ai_review.issue_lifecycle.adopt_history'), \
         patch('ai_review.batch_counter.run', side_effect=ReviewError('测试在计数前停止')) as counter, \
         patch('ai_review.native_review.execute') as model:
        batch_worker.process(store, job)
    payload = counter.call_args.args[1]
    assert payload['flow_version'] == version
    assert payload['snapshot'].get('batch_evidence_encoding') == encoding
    assert payload['snapshot'].get('batch_output_protocol') == (batch_locations.PHASE_PROTOCOL if version >= 8 else None)
    assert store.job(job['id'])['snapshot'].get('batch_evidence_encoding') == encoding
    model.assert_not_called()


def test_compact_refutation_and_correction_keep_exact_candidates_and_report():
    snap, value = plan(True)
    item = value['units'][0]
    assert item['evidence_encoding'] == batch_locations.FILE_LINES_ENCODING
    saved = completed(value)
    saved['units'][0]['result'] = batch_plan.parse_report(raw({'findings': [finding()]}), snap, item)
    groups = batch_flow.refutations(snap, saved, config(50000))
    assert len(groups) == 1
    group = groups[0]
    assert group['evidence_encoding'] == batch_locations.FILE_LINES_ENCODING
    message = json.loads(batch_plan.messages(snap, group, group['candidates'])[1]['content'])
    rows, candidates = batch_locations.payload(group, group['candidates'])
    assert decode(message['evidence']) == rows and message['candidates'] == candidates
    previous = {'findings': [finding(line=999)]}
    retry = json.loads(batch_plan.messages(snap, item, correction=previous)[1]['content'])
    assert retry['previous_report'] == previous
    assert decode(retry['evidence']) == batch_locations.payload(item)[0]


@pytest.mark.parametrize('change', [{'path': 'unknown.c'}, {'side': 'old'}, {'line': 999}])
def test_compact_input_does_not_relax_position_validation(change):
    snap, value = plan(True)
    with pytest.raises(ReviewError):
        batch_plan.parse_report(raw({'findings': [finding(**change)]}), snap, value['units'][0])


def test_one_correction_lists_all_invalid_locations_without_selecting_replacements():
    snap, value = plan(True)
    item = value['units'][0]
    item['evidence'].append(dict(item['evidence'][0], id='空行', new=2, text='\n', anchor_id=None))
    previous = {'findings': [finding(line=2), finding(line=99),
                            finding(related_locations=[{'path': 'absent.c', 'side': 'new', 'line': 1}])]}
    payload = json.loads(batch_plan.messages(snap, item, correction=previous)[1]['content'])
    assert payload['previous_report'] == previous
    assert payload['position_errors'] == [
        {'item_index': 1, 'field': 'location', 'reason': 'source_location_uncommentable',
         'location': {'path': 'test.c', 'side': 'new', 'line': 2}},
        {'item_index': 2, 'field': 'location', 'reason': 'source_location_unknown',
         'location': {'path': 'test.c', 'side': 'new', 'line': 99}},
        {'item_index': 3, 'field': 'related_locations[0]', 'reason': 'source_location_unknown',
         'location': {'path': 'absent.c', 'side': 'new', 'line': 1}}]
    assert batch_locations.correction_errors({'findings': [finding(line=99), None]}, item)[0]['item_index'] == 1
    legacy = dict(item); legacy.pop('evidence_encoding')
    assert 'position_errors' not in json.loads(batch_plan.messages(snap, legacy, correction=previous)[1]['content'])


def test_complete_count_is_of_encoded_request_and_metadata_cost_is_reduced():
    snap = snapshot('int f(void) {\n' + '    value++;\n' * 150 + '}\n')
    snap.update(batch_reference_protocol=batch_locations.PROTOCOL,
                batch_evidence_encoding=batch_locations.FILE_LINES_ENCODING)
    cfg = config(500000)
    cfg['model_context_tokens'] = 1048576
    value = batch_flow.build(snap, cfg, single_request=True)
    item = value['units'][0]
    actual = token_budget.count(batch_plan.messages(snap, item), cfg)
    assert item['input_tokens'] == actual
    legacy = dict(item)
    legacy.pop('evidence_encoding')
    assert actual < token_budget.count(batch_plan.messages(snap, legacy), cfg) * .5

