"""使用现场记录中的行号与字节构造独立回归样例。"""

import pytest

from ai_review.common import ReviewError
from ai_review.issue_lifecycle import validate_check


def check(payload, entries, protocol=3):
    raw = {
        'issue_id': '历史问题',
        'snapshot_id': '固定版本',
        'status': 'present',
        'reason': '固定语句仍在当前版本。',
        'relations': {},
        'anchor_id': None,
        'evidence': entries,
    }
    return validate_check(raw, {
        'issue_id': '历史问题', 'candidates': [], 'historical_diff': {}, 'current_diff': {},
        **payload,
    }, {'id': '固定版本', 'pr_agent_protocol': protocol})


def test_history_source_indent_is_restored_from_fixed_source():
    line = 'mng->status = TRIM_STATE_IDLE;'
    payload = {'current_source': {'S857': ' ' * 16 + line}}
    model = [{'id': 'S857', 'quote': ' ' * 12 + line}]
    with pytest.raises(ReviewError):
        check(payload, model, protocol=2)
    assert check(payload, model)['evidence'] == [{'id': 'S857', 'quote': ' ' * 16 + line}]
    with pytest.raises(ReviewError):
        check(payload, [{'id': 'S857', 'quote': line.replace('IDLE', 'BUSY')}])


def test_shifted_current_ids_are_recovered_from_unique_code_in_the_same_hunk():
    patch = {
        'C400': 'diff --git a/check.py b/check.py',
        'C401': '@@ -10,100 +10,100 @@',
        'C428': '+    unrelated = True',
        'C486': '+    selected_files = summary["files"]',
        'C506': '+    guard = verify_payload()',
        'C509': '+    for filename, item in summary["files"].items():',
        'C513': '+        payload = create_payload(filename, item)',
        'C515': '+        save_result(filename, payload)',
    }
    mapping = {'C405': 'C486', 'C428': 'C509', 'C432': 'C513', 'C434': 'C515', 'C506': 'C506'}
    model = [{'id': old, 'quote': patch[new]} for old, new in mapping.items()]
    with pytest.raises(ReviewError):
        check({'current_diff': patch}, model, protocol=2)
    verified = check({'current_diff': patch}, model)
    assert verified['evidence'] == [{'id': new, 'quote': patch[new]} for new in mapping.values()]


@pytest.mark.parametrize('other_line', [
    {'C510': '+    save_record(file_name)'},
    {'C502': '@@ -150 +150 @@', 'C510': '+    save_record(file_name)'},
    {'C502': 'diff --git a/other.py b/other.py', 'C503': '@@ -2 +2 @@',
     'C510': '+    save_record(file_name)'},
])
def test_shifted_id_cannot_bind_to_a_duplicate_or_to_another_hunk(other_line):
    patch = {
        'C400': 'diff --git a/check.py b/check.py',
        'C401': '@@ -10,100 +10,100 @@',
        'C428': '+    unrelated = True',
        **other_line,
    }
    if 'C502' not in other_line:
        patch['C509'] = '+    save_record(file_name)'
    with pytest.raises(ReviewError):
        check({'current_diff': patch}, [{'id': 'C428', 'quote': '+    save_record(file_name)'}])
