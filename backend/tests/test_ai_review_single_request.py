"""默认一次主审与千行边界；历史分批计划仍保持原路径。"""
import json
from unittest.mock import patch

import pytest

from ai_review import batch_counter, batch_flow, batch_plan, batch_store, token_budget
from ai_review.common import ReviewError
from test_ai_review_batches import state
from test_ai_review_batch_flow import snapshot, config, completed, example_candidate


def thousand_lines(count=1000):
    return 'int first(void) {\n' + '    int x = 1;\n' * (count - 3) + '    return 0;\n}\n'


def test_thousand_lines_are_one_complete_request():
    snap = snapshot(thousand_lines())
    value = batch_flow.build(snap, dict(config(200000), model_context_tokens=1048576), single_request=True)
    assert value['review_mode'] == 'single'
    assert value['net_added_lines'] == 1000 and value['max_changed_lines'] == 1200
    assert value['primary'] == 1 and value['association'] == 0
    assert value['changed_lines'] == 1000 and len(value['units'][0]['core']) == 1000
    assert not value['gaps']
    assert '一次审查完整 PR' in batch_plan.messages(snap, value['units'][0])[0]['content']


def test_twelve_hundred_and_one_lines_fail_before_analysis_or_model():
    with patch('ai_review.batch_graph.analyze') as analyze:
        with pytest.raises(ReviewError) as error:
            batch_flow.build(snapshot(thousand_lines(1201)), dict(config(200000), model_context_tokens=1048576), single_request=True)
    assert error.value.error_code == 'PR_TOTAL_CHANGE_LIMIT'
    analyze.assert_not_called()


def test_deletions_offset_additions_across_files():
    snap = snapshot('int a;\n' * 500)
    snap['diff'] += 'diff --git a/old.c b/old.c\n--- a/old.c\n+++ /dev/null\n@@ -1,501 +0,0 @@\n' + '-int b;\n' * 501
    counts = batch_flow.validate_single_size(snap)
    assert counts == {'added_lines': 500, 'deleted_lines': 501, 'net_added_lines': 0}


def test_below_line_limit_but_over_token_budget_does_not_split():
    with pytest.raises(ReviewError) as error:
        batch_flow.build(snapshot(thousand_lines()), config(22000), single_request=True)
    assert error.value.error_code == 'MODEL_INPUT_BUDGET'


def test_all_related_protection_is_in_the_single_request():
    text = 'int divisor;\nint divide(void) { return 10 / divisor; }\nint caller(void) { if (divisor == 0) return 0; return divide(); }\n'
    snap = snapshot(text)
    # 只修改被调用方，其他函数是固定版本保护上下文。
    snap['diff'] = 'diff --git a/test.c b/test.c\n--- a/test.c\n+++ b/test.c\n@@ -2 +2 @@\n-int divide(void) { return 8 / divisor; }\n+int divide(void) { return 10 / divisor; }\n'
    snap['analysis_sources'][0]['text'] = text
    value = batch_flow.build(snap, config(50000), single_request=True)
    assert value['primary'] == 1 and value['association'] == 0
    assert any('if (divisor == 0)' in r['text'] for r in value['units'][0]['evidence'])
    assert all(r.get('covered_by') for r in value['relations'])


def test_single_refutation_keeps_all_candidates_and_fails_instead_of_splitting():
    snap = snapshot('int f(void) { return 0; }\n');cfg = config(50000)
    value = batch_flow.build(snap, cfg, single_request=True);saved = completed(value)
    a = example_candidate(value['units'][0]);b = dict(a, id=a['id']+'b', title='另一问题')
    saved['units'][0]['result']['findings'] = [a, b]
    groups = batch_flow.refutations(snap, saved, cfg)
    assert len(groups) == 1 and len(groups[0]['candidates']) == 2
    assert groups[0]['review_mode'] == 'single'
    assert {r['id'] for r in groups[0]['evidence']} == {r['id'] for r in value['units'][0]['evidence']}
    with patch('ai_review.batch_flow.count', return_value=50000):
        with pytest.raises(ReviewError) as error:
            batch_flow.refutations(snap, saved, cfg)
    assert error.value.error_code == 'MODEL_INPUT_BUDGET'


def test_single_request_never_uses_automatic_output_splitting(state):
    store, job, _ = state
    record = {'value': {'review_mode': 'single', 'flow_version': batch_flow.VERSION}}
    job['batch_flow_version'] = 5
    assert not batch_store.adjust_output(store, job, record, {'finish_reason': 'length'}, {}, {})


@pytest.mark.parametrize('version,enabled,expected_single', [(7, False, True), (7, True, False), (6, False, True), (6, True, False), (5, False, True), (5, True, False), (4, False, False)])
def test_counter_selects_new_default_and_keeps_legacy_plan(version, enabled, expected_single):
    request = {'operation': 'plan', 'config': {'batch_splitting_enabled': enabled},
               'payload': {'flow_version': version, 'snapshot': snapshot('int value;\n'), 'history': []}}
    import io
    with patch('sys.stdin', io.StringIO(json.dumps(request))), patch('ai_review.batch_flow.build', return_value={}) as build, patch('builtins.print'):
        batch_counter.main()
    assert build.call_args.kwargs == {'single_request': expected_single, 'net_limit': version == 5}


def test_split_switch_is_false_by_default_and_frozen_per_job(state):
    store, job, repo = state
    assert token_budget.DEFAULTS['batch_splitting_enabled'] is False
    assert store.config()['batch_splitting_enabled'] is False
    first = store.enqueue(repo['id'], 21, {'head': 'c'*40, 'base': 'b'*40})
    store.save_config({'revision': store.config()['revision'], 'batch_splitting_enabled': True})
    assert store.job(first)['status'] == 'queued'
    assert store.job_config(first)['batch_splitting_enabled'] is False
    second = store.enqueue(repo['id'], 22, {'head': 'd'*40, 'base': 'b'*40})
    assert store.job_config(second)['batch_splitting_enabled'] is True
    assert store.job(first)['batch_flow_version'] == 7


def test_split_switch_rejects_non_boolean():
    with pytest.raises(ReviewError, match='布尔值'):
        token_budget.validate({'batch_splitting_enabled': 'false'})


def test_net_limit_does_not_limit_total_review_coverage():
    snap = snapshot('int a;\n' * 1500)
    snap['diff'] += 'diff --git a/old.c b/old.c\n--- a/old.c\n+++ /dev/null\n@@ -1,500 +0,0 @@\n' + '-int b;\n' * 500
    value = batch_flow.build(snap, dict(config(1000000), model_context_tokens=1048576), single_request=True, net_limit=True)
    assert value['net_added_lines'] == 1000
    assert value['max_net_added_lines'] == 1000
    assert value['added_lines'] == 1500 and value['deleted_lines'] == 500
    assert value['changed_lines'] == 2000 and len(value['units'][0]['core']) == 2000
    assert value['primary'] == 1 and value['association'] == 0


def test_single_request_keeps_unmodified_diff_context():
    snap = snapshot('int f(void) { return 2; }\nint unrelated(void) { return 7; }\n')
    snap['diff'] = 'diff --git a/test.c b/test.c\n--- a/test.c\n+++ b/test.c\n@@ -1,2 +1,2 @@\n-int f(void) { return 1; }\n+int f(void) { return 2; }\n int unrelated(void) { return 7; }\n'
    value = batch_flow.build(snap, config(50000), single_request=True)
    assert any('int unrelated(void)' in r['text'] for r in value['units'][0]['evidence'])
    assert value['changed_lines'] == 2


def test_twelve_hundred_total_lines_are_allowed():
    value = batch_flow.build(snapshot(thousand_lines(1200)),
                             dict(config(250000), model_context_tokens=1048576), single_request=True)
    assert value['changed_lines'] == 1200 and value['primary'] == 1
    assert value['max_changed_lines'] == 1200


def test_net_small_but_total_large_is_rejected():
    snap = snapshot('int a;\n' * 701)
    snap['diff'] += 'diff --git a/old.c b/old.c\n--- a/old.c\n+++ /dev/null\n@@ -1,500 +0,0 @@\n' + '-int b;\n' * 500
    with pytest.raises(ReviewError) as error:
        batch_flow.validate_single_size(snap)
    assert error.value.error_code == 'PR_TOTAL_CHANGE_LIMIT'


def test_current_hard_limit_cannot_be_bypassed_by_split_switch():
    import io
    request = {'operation': 'plan', 'config': {'batch_splitting_enabled': True},
               'payload': {'flow_version': 6, 'snapshot': snapshot(thousand_lines(1201)), 'history': []}}
    with patch('sys.stdin', io.StringIO(json.dumps(request))), patch('ai_review.batch_flow.build') as build, patch('builtins.print') as emit:
        batch_counter.main()
    build.assert_not_called()
    assert json.loads(emit.call_args.args[0])['code'] == 'PR_TOTAL_CHANGE_LIMIT'
