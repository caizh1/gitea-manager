"""固定 Git 对象与混合编码的局部覆盖边界。"""

import hashlib
import subprocess

import pytest

from ai_review.common import ReviewError
from ai_review.native_workspace import bind_snapshot_evidence
from ai_review.pr_agent_encoding import PROTOCOL, decode_blob, pack
from ai_review.pr_agent_review import phase_input
from ai_review.store import Store


def test_mixed_utf8_regions_cannot_be_silently_interpreted_as_gb18030():
    raw = '中文'.encode() + b'\xa1\xa2\n'
    with pytest.raises(ReviewError, match='混合编码歧义'):
        decode_blob(raw, 'sample.c', '新侧', {'.': 'gb18030'}, reject_mixed=True)
    assert decode_blob('旧版注释\r\n'.encode('gbk'), 'sample.c', '旧侧', {'.': 'gbk'},
                       reject_mixed=True) == ('旧版注释\r\n', 'gbk')


def test_partial_diff_keeps_verified_files_and_rejects_omissions(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args])

    git('init', '-q')
    git('config', 'user.name', '验收')
    git('config', 'user.email', 'review@example.invalid')
    paths = {'text.py': b'value = ', 'legacy.py': b'# \xe4\xbb?\nvalue = ', 'binary.bin': b'\x00'}
    for index in (1, 2):
        for path, prefix in paths.items():
            (repo / path).write_bytes(prefix + str(index).encode() + b'\n')
        git('add', '--all')
        git('commit', '-qm', str(index))
    base = git('rev-parse', 'HEAD^').decode().strip()
    head = git('rev-parse', 'HEAD').decode().strip()
    store = Store(tmp_path / 'review.db')
    store.save_config(dict(store.config(), enabled=True, debounce_seconds=0))
    configured = store.save_repo(1, 1, {'enabled': True, 'repo_name': 'owner/repo'})
    store.enqueue(configured['id'], 1, {'head': head, 'base': base})
    job = store.claim()

    def snapshot(raw, count):
        return {'head': head, 'merge_base': base, 'title': '测试', 'description': '',
                'id': '固定快照', 'source_encodings': {}, 'max_changed_lines': 30000,
                'workspace_layout': 'git-worktree-v2', 'pr_agent_protocol': PROTOCOL,
                'raw_diff': pack(raw), 'diff_sha256': hashlib.sha256(raw).hexdigest(),
                'coverage': {'files_total': count}}

    raw = git('diff', base, head)
    result = bind_snapshot_evidence(store, job, snapshot(raw, 3), repo)
    assert {item['path'] for item in result['coverage']['skipped']} == {'legacy.py', 'binary.bin'}
    assert result['coverage']['files_reviewed'] == 1
    assert not result['coverage']['complete']
    result['review_rules'] = {'status': 'absent'}
    assert phase_input(result, 'primary')['review_paths'] == ['text.py']
    with pytest.raises(ReviewError, match='文件范围'):
        bind_snapshot_evidence(store, job, snapshot(git('diff', base, head, '--', 'text.py'), 1), repo)
