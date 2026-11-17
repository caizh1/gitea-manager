"""原生反证定位漂移和复核级别的回归，重现相邻起始行的现场。"""
import copy

import pytest

from ai_review.codex_review import IncompleteReview, normalize, prompt, refutation_result
from test_auto_publication import case
from test_ai_review import make_diff
from ai_review.diff_inline import index
from ai_review.common import encode

BASE = '\n'.join([
    '#include <stddef.h>', '', '', '', '', '', '', '',
    'void copy(char *dst, const char *src, size_t len) {',
    '    for (size_t i = 0; i < len; i++) {',
    '        dst[i] = 0;', '    }', '}', '',
])
HEAD = BASE.replace('i < len', 'i <= len').replace('dst[i] = 0', 'dst[i] = src[i]')


def raw(start=11, end=12, priority=1, path='after/code.c'):
    return {'findings': [{
        'priority': priority, 'title': f'[P{priority}] 复制循环越过有效范围',
        'body': '当长度等于缓冲区容量时，额外一次迭代会访问索引 len，越过有效范围。',
        'code_location': {'absolute_file_path': path, 'line_range': {'start': start, 'end': end}},
    }], 'overall_explanation': '已核对循环边界、调用约束及缓冲区容量。'}


@pytest.fixture
def sample(tmp_path):
    (tmp_path / 'after').mkdir()
    (tmp_path / 'before').mkdir()
    (tmp_path / 'after/code.c').write_text(HEAD)
    (tmp_path / 'after/other.c').write_text(HEAD)
    (tmp_path / 'before/code.c').write_text(BASE)
    snapshot = {'id': '固定源码', 'base': 'b' * 40, 'head': 'a' * 40, 'merge_base': 'b' * 40,
                'codex_tools': 'native', 'engine': 'codex_cli', 'review_scope': 'diff',
                'files': [{'path': 'code.c', 'old': BASE, 'new': HEAD}],
                'diff': make_diff(BASE, HEAD).decode(),
                'coverage': {'complete': True, 'files_total': 1, 'files_reviewed': 1, 'skipped': []}}
    snapshot['anchors'], _ = index(snapshot)
    candidate = normalize(raw(), snapshot, tmp_path)[0]
    return tmp_path, snapshot, candidate


def test_overlapping_locations_keep_primary_anchor_and_use_reviewed_priority(sample):
    workspace, snapshot, candidate = sample
    original = copy.deepcopy(candidate)
    finding, audit = refutation_result(raw(10, 11, 2), snapshot, workspace, candidate)
    assert candidate == original
    assert finding['severity'] == 'P2'
    assert finding['anchor'] == original['anchor']
    assert finding['support'] == original['support']
    assert finding['title'].startswith('[P2]')
    assert audit['location_adjusted'] is True
    assert audit['original_location']['line_start'] == 11
    assert audit['reviewed_location']['line_start'] == 10
    assert (audit['severity_before'], audit['severity_after']) == ('P1', 'P2')


def test_same_location_is_retained_without_adjustment(sample):
    workspace, snapshot, candidate = sample
    finding, audit = refutation_result(raw(), snapshot, workspace, candidate)
    assert finding['severity'] == 'P1'
    assert audit['retained'] and not audit['location_adjusted']


@pytest.mark.parametrize('result', [raw(4, 5), raw(10, 11, path='after/other.c'), raw(10, 11, path='before/code.c')])
def test_unrelated_location_or_source_side_is_rejected(sample, result):
    workspace, snapshot, candidate = sample
    with pytest.raises(IncompleteReview, match='不相交'):
        refutation_result(result, snapshot, workspace, candidate)


def test_rejected_candidate_does_not_create_finding(sample):
    workspace, snapshot, candidate = sample
    finding, audit = refutation_result({'findings': [], 'overall_explanation': '调用方始终额外分配一个元素。'},
                                       snapshot, workspace, candidate)
    assert finding is None
    assert audit['retained'] is False


def test_multiple_findings_cannot_replace_one_candidate(sample):
    workspace, snapshot, candidate = sample
    result = raw()
    result['findings'].append(raw(10, 11)['findings'][0])
    with pytest.raises(IncompleteReview, match='多个问题'):
        refutation_result(result, snapshot, workspace, candidate)


@pytest.mark.parametrize('layout', ['git-worktree-v1', 'git-worktree-v2'])
def test_native_refutation_prompt_keeps_native_result_and_explains_priority(sample, layout):
    _, snapshot, candidate = sample
    text = prompt(dict(snapshot, workspace_layout=layout), 'refutation-0', [candidate])
    assert '尽量沿用原代码定位' in text
    assert '调整 priority' in text
    assert '不新增其他问题' in text
    assert '复制循环越过有效范围' in text


@pytest.mark.parametrize('saved', [False, True])
@pytest.mark.parametrize('priority', [1, 2])
def test_process_publishes_shifted_finding_and_reuses_saved_phases(case, sample, monkeypatch, saved, priority):
    from ai_review import codex_review

    workspace, snapshot, _ = sample
    snapshot.update(head=case.snapshot['head'], base=case.snapshot['base'], merge_base=case.snapshot['merge_base'])
    case.snapshot = snapshot
    case.client.snapshot = snapshot
    case.store.update(case.job['id'], case.job['owner'], snapshot=snapshot)
    case.job = case.store.job(case.job['id'])
    phases = {'primary': raw(), 'refutation-0': raw(10, 11, priority)}
    for value in phases.values():
        value.update(_native_result_sha256='固定回执', _native_final_event_seq=1)
    if saved:
        with case.store.tx() as connection:
            for phase, value in phases.items():
                connection.execute('INSERT INTO codex_review_phases VALUES (?,?,?,?,?)',
                                   (case.job['id'], phase, phase, encode(value), 1))
    calls = []
    finish = case.store.finish

    def checked_finish(job, owner, status, error=''):
        if status == 'failed':
            raise AssertionError(error)
        return finish(job, owner, status, error)

    monkeypatch.setattr(case.store, 'finish', checked_finish)

    def phase(_store, _job, _config, _snapshot, _workspace, name, candidates=None):
        assert not saved, '已保存的原生阶段不应重新调用模型'
        calls.append(name)
        return phases[name]

    monkeypatch.setattr(codex_review, 'run_phase', phase)
    monkeypatch.setattr('ai_review.worker.client_for', lambda *_: case.client)
    monkeypatch.setattr('ai_review.worker.refresh_pr', lambda *_: None)
    monkeypatch.setattr('ai_review.git_cache.prepare', lambda *_: (str(workspace), {}))
    monkeypatch.setattr('ai_review.repository_rules.freeze', lambda _s, _j, snap, _w: snap)
    monkeypatch.setattr('ai_review.native_workspace.bind_snapshot_evidence', lambda _s, _j, snap, _w: snap)
    monkeypatch.setattr('ai_review.native_review.acknowledge_result', lambda *_: None)
    codex_review.process(case.store, case.job)
    completed = case.store.job(case.job['id'])
    assert completed['status'] == 'succeeded', completed['error']
    verdict = 'FAIL' if priority == 1 else 'PASS'
    assert completed['result']['report_markdown'].startswith('VERDICT: ' + verdict)
    finding = completed['result']['findings'][0]
    assert finding['severity'] == 'P' + str(priority) and finding['anchor']['line'] == 11
    assert completed['result']['refutation'][0]['reviewed_location']['line_start'] == 10
    assert completed['publication_status']['comments_published'] == 1
    assert len(case.client.reviews) == 1
    assert len(case.client.summaries) == 1
    assert 'VERDICT: ' + verdict in case.client.summaries[0]['body']
    assert calls == ([] if saved else ['primary', 'refutation-0'])
