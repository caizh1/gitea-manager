"""固定目标规则的版本、输入、恢复和访问边界回归。"""
import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_review.common import ReviewError
from ai_review.repository_rules import PATH, MAX_BYTES, freeze, check_phase, public, check_input
from ai_review.codex_review import prompt
from ai_review.store import Store


class MemoryStore:
    def __init__(self):
        self.saved = None

    def active(self, *_):
        return True

    def update(self, *_, **values):
        self.saved = values['snapshot']


def git(folder, *args):
    return subprocess.check_output(['git', '-C', str(folder), *args]).decode().strip()


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.email', 'rules@example.invalid')
    git(tmp_path, 'config', 'user.name', '规则验收')
    (tmp_path / '.gitea').mkdir()
    return tmp_path


def commit(folder, raw=None):
    path = folder / PATH
    if raw is not None:
        path.write_bytes(raw)
    git(folder, 'add', '-A')
    git(folder, 'commit', '--allow-empty', '-qm', '规则验收提交')
    return git(folder, 'rev-parse', 'HEAD')


def snapshot(base, head=None):
    return {'id': '原始快照', 'base': base, 'head': head or base, 'merge_base': base,
            'workspace_layout': 'git-worktree-v1', 'diff': '测试变更原文'}


def load(folder, base, head=None):
    state = MemoryStore()
    value = freeze(state, {'id': '任务', 'owner': '持有者', 'rules_protocol': 1}, snapshot(base, head), folder)
    return value, state


@pytest.mark.parametrize('raw,status', [(None, 'missing'), (b'', 'empty'), (b' \r\n\t', 'empty'),
    ('\ufeff \r\n'.encode(), 'empty'), ('\ufeff# 用户规则\r\n- 遵守接口契约\r\n'.encode(), 'loaded')])
def test_encoding_and_absence(repository, raw, status):
    base = commit(repository, raw)
    value, state = load(repository, base)
    rules = value['review_rules']
    assert rules['status'] == status
    assert rules['sha256'] == (hashlib.sha256(raw).hexdigest() if raw is not None else None)
    assert rules['content'].encode() == (raw or b'')
    assert state.saved == value


@pytest.mark.parametrize('raw', [b'x' * (MAX_BYTES + 1), b'\xff', b'a\0b',
    b'version https://git-lfs.github.com/spec/v1\noid sha256:123\nsize 9\n'])
def test_invalid_file_is_persisted_failure(repository, raw):
    base = commit(repository, raw)
    state = MemoryStore()
    with pytest.raises(ReviewError):
        freeze(state, {'id': '任务', 'owner': '持有者', 'rules_protocol': 1}, snapshot(base), repository)
    assert state.saved['review_rules']['status'] == 'failed'
    with pytest.raises(ReviewError):
        freeze(state, {'id': '任务', 'owner': '持有者', 'rules_protocol': 1}, state.saved, repository)


@pytest.mark.parametrize('kind', ['symlink', 'directory', 'submodule'])
def test_non_regular_files_are_rejected(repository, kind):
    path = repository / PATH
    if kind == 'symlink':
        path.symlink_to('/etc/passwd')
    elif kind == 'directory':
        path.mkdir()
        (path / 'nested.md').write_text('测试')
    else:
        first = commit(repository)
        git(repository, 'update-index', '--add', '--cacheinfo', '160000,' + first + ',' + PATH)
    base = commit(repository) if kind != 'submodule' else (git(repository, 'commit', '-qm', '子模块规则') or git(repository, 'rev-parse', 'HEAD'))
    with pytest.raises(ReviewError):
        load(repository, base)


@pytest.mark.parametrize('change', ['edit', 'delete', 'add'])
def test_only_base_is_used_and_resume_is_frozen(repository, change):
    ancestor = commit(repository, '祖先规则'.encode() if change != 'add' else None)
    base = commit(repository, '批准的目标规则'.encode() if change != 'add' else None)
    if change == 'delete':
        (repository / PATH).unlink()
        head = commit(repository)
    else:
        head = commit(repository, '未批准的头部规则'.encode())
    source = snapshot(base, head)
    source['merge_base'] = ancestor
    state = MemoryStore()
    value = freeze(state, {'id': '任务', 'owner': '持有者', 'rules_protocol': 1}, source, repository)
    assert value['review_rules']['content'] == ('批准的目标规则' if change != 'add' else '')
    with patch('ai_review.repository_rules.run_git', side_effect=AssertionError('恢复不能重新读取')):
        assert freeze(state, {'id': '任务', 'owner': '持有者', 'rules_protocol': 1}, value, repository) == value
    if change != 'add':
        for phase in ('primary', 'refutation-0', 'history-原问题'):
            text = prompt(value, phase, [] if phase.startswith('refutation') else None)
            assert '批准的目标规则' in text
            assert '未批准的头部规则' not in text
            assert '不能作为代码已经修复的证据' in text
    check_phase(value, {'_rules_identity': value['rules_identity']})
    with pytest.raises(ReviewError):
        check_phase(value, {'_rules_identity': '其他规则'})
    with pytest.raises(ReviewError):
        check_phase(value, {})
    tampered = dict(value, base=head)
    with pytest.raises(ReviewError):
        freeze(state, {'id': '任务', 'owner': '持有者', 'rules_protocol': 1}, tampered, repository)


def test_size_boundary_and_read_failure(repository):
    base = commit(repository, b'x' * MAX_BYTES)
    assert load(repository, base)[0]['review_rules']['status'] == 'loaded'
    with patch('ai_review.repository_rules.run_git', side_effect=ReviewError('Git 对象读取失败')):
        with pytest.raises(ReviewError):
            load(repository, base)


def test_old_tasks_and_input_budget(repository):
    original = snapshot('a' * 40)
    with patch('ai_review.repository_rules.run_git', side_effect=AssertionError('历史任务不读取')):
        assert freeze(MemoryStore(), {'rules_protocol': 0}, original, repository) == original
    assert public({'rules_protocol': 0})['status'] == 'legacy'
    check_input('规则', {'model_context_tokens': 32768})
    with pytest.raises(ReviewError):
        check_input('规则' * 5000, {'model_context_tokens': 32768})


def test_migration_is_idempotent_and_enqueue_version_is_explicit(tmp_path):
    state = Store(tmp_path / 'state.db')
    state.save_config({'revision': state.config()['revision'], 'enabled': True})
    repo = state.save_repo(1, 1, {'enabled': True})
    with patch('ai_review.runtime_status.available', return_value=True):
        ident = state.enqueue(repo['id'], 1, {'base': 'a' * 40, 'head': 'b' * 40})
    assert state.job(ident)['rules_protocol'] == 1
    with state.tx() as connection:
        connection.execute('ALTER TABLE jobs DROP COLUMN rules_protocol')
        connection.execute('PRAGMA user_version=20')
    state = Store(state.path)
    assert state.job(ident)['rules_protocol'] == 0
    Store(state.path)
    assert state.job(ident)['rules_protocol'] == 0


def test_rules_access_is_scoped():
    from tests.test_organization_access import OrganizationAccessTest
    fixture = OrganizationAccessTest()
    fixture.setUp()
    try:
        path = '/api/ai-review/tasks/'
        assert fixture.request(path + 'a' * 32 + '/rules').status_code == 200
        assert fixture.request(path + 'b' * 32 + '/rules').status_code == 404
        fixture.client.post('/api/logout', base_url='https://localhost')
    finally:
        fixture.tearDown()
        fixture.doCleanups()


def test_real_stage_entry_forwards_identical_frozen_rules(repository, tmp_path):
    from ai_review.codex_review import run_phase
    state = Store(tmp_path / 'stage.db')
    state.save_config({'revision': state.config()['revision'], 'enabled': True, 'debounce_seconds': 0})
    repo = state.save_repo(1, 1, {'enabled': True})
    base = commit(repository, '# 用户约束\n检查租户隔离。'.encode())
    with patch('ai_review.runtime_status.available', return_value=True):
        ident = state.enqueue(repo['id'], 1, {'base': base, 'head': base})
    job = state.claim()
    frozen = freeze(state, job, snapshot(base), repository)
    sent = []
    def execute(*args, **kwargs):
        sent.append(kwargs['diff_snapshot']['_batch_messages'][1]['content'])
        return {'findings': [], '_native_result_sha256': '固定回执', '_native_final_event_seq': 1, '_event_log_complete': True}
    with patch('ai_review.native_review.execute', side_effect=execute), patch('ai_review.native_review.acknowledge_result'):
        for phase, candidates in [('primary', None), ('refutation-0', []),
                                  ('history-1', {'instructions': '核对历史源码', 'payload': {}})]:
            result = run_phase(state, job, state.config(), frozen, str(repository), phase, candidates)
            assert result['_rules_identity'] == frozen['rules_identity']
    assert len(sent) == 3
    assert all('检查租户隔离。' in text for text in sent)
    detail = state.job(ident, public=True)
    assert 'content' not in detail['review_rules']
    assert 'review_rules' not in detail['snapshot']
    assert public(state.job(ident), {'gitea_url': 'https://gitea.invalid', 'repo_name': 'team/project'}, True)['content'] == '# 用户约束\n检查租户隔离。'


def test_rules_digest_changes_without_head_change(repository):
    first = commit(repository, '第一版'.encode())
    second = commit(repository, '第二版'.encode())
    old, _ = load(repository, first, first)
    new, _ = load(repository, second, first)
    assert old['id'] != new['id']
    assert old['rules_identity'] != new['rules_identity']
    new['review_rules']['content'] = '被替换的正文'
    with pytest.raises(ReviewError):
        freeze(MemoryStore(), {'rules_protocol': 1}, new, repository)
