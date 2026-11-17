"""并发领取、动态容量、停止确认与同仓库固定版本隔离回归。"""
import concurrent.futures
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_review.store import Store
from ai_review import concurrency, git_workspace, native_workspace


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    key = tmp_path / 'master.key'
    key.write_bytes(Fernet.generate_key())
    monkeypatch.setenv('AI_REVIEW_MASTER_KEY_FILE', str(key))
    monkeypatch.setattr(native_workspace, 'WORKSPACE_ROOT', tmp_path / 'workspaces')
    store = Store(tmp_path / 'review.db')
    config = store.config()
    config.update(enabled=True, engine='codex_cli', debounce_seconds=0)
    store.save_config(config)
    repo = store.save_repo(1, 1, {'enabled': True, 'repo_name': 'test/code', 'gitea_url': 'http://test', 'bot_token': store.seal('测试')})
    return store, repo


def enqueue(store, repo, number):
    return store.enqueue(repo['id'], number, {'head': str(number), 'base': '0'})


def test_atomic_five_slots_and_resize(setup):
    store, repo = setup
    for number in range(10):
        enqueue(store, repo, number)
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        jobs = [job for job in pool.map(lambda _: store.claim(), range(20)) if job]
    assert len(jobs) == 5
    assert len({job['id'] for job in jobs}) == 5
    config = store.config()
    execution_revision = config['execution_revision']
    config['max_concurrent_reviews'] = 2
    store.save_config(config)
    assert store.config()['execution_revision'] == execution_revision
    assert store.claim() is None
    for job in jobs[:4]:
        store.finish(job['id'], job['owner'], 'succeeded')
    concurrency.reap(store)
    assert concurrency.status(store)['occupied'] == 1
    assert store.claim()
    assert store.claim() is None
    config = store.config()
    config['max_concurrent_reviews'] = 5
    store.save_config(config)
    assert sum(store.claim() is not None for _ in range(5)) == 3


def test_old_generation_and_unknown_stop_keep_only_their_slot(setup, monkeypatch):
    store, repo = setup
    enqueue(store, repo, 1)
    old = store.claim()
    store.update(old['id'], old['owner'], session_id='a' * 32)
    store.enqueue(repo['id'], 1, {'head': 'updated', 'base': '0'})
    enqueue(store, repo, 2)
    other = store.claim()
    assert other['number'] == 2
    assert store.claim() is None
    monkeypatch.setattr('ai_review.native_review.cancel_session', lambda *a: {'execution_stopped': False})
    concurrency.reap(store)
    assert concurrency.status(store)['occupied'] == 2
    monkeypatch.setattr('ai_review.native_review.cancel_session', lambda *a: {'execution_stopped': True})
    concurrency.reap(store)
    assert store.claim()['number'] == 1


def test_expired_lease_requires_execution_exit(setup):
    store, repo = setup
    enqueue(store, repo, 1)
    job = store.claim()
    with store.tx() as connection:
        connection.execute('UPDATE jobs SET lease=? WHERE id=?', (time.time() - 1, job['id']))
    with store.lock('execution-' + job['id']):
        concurrency.reap(store)
        assert store.claim() is None
    concurrency.reap(store)
    recovered = store.claim()
    assert recovered['id'] == job['id']
    assert recovered['owner'] != job['owner']


def test_upgrade_default_and_validation(setup):
    store, _ = setup
    with store.tx() as connection:
        saved = json.loads(connection.execute('SELECT value FROM settings').fetchone()[0])
        saved.pop('max_concurrent_reviews')
        connection.execute('UPDATE settings SET value=?', (json.dumps(saved),))
        connection.execute('PRAGMA user_version=21')
    upgraded = Store(store.path)
    assert upgraded.config()['max_concurrent_reviews'] == 5


def test_five_worktrees_keep_their_own_head(setup, tmp_path, monkeypatch):
    store, repo = setup
    source = tmp_path / 'source'
    source.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(source), *args], stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-b', 'main')
    git('config', 'user.name', '验收')
    git('config', 'user.email', 'test@example.invalid')
    (source / 'value.py').write_text('value = 0\n')
    git('add', '.')
    git('commit', '-m', '基线')
    base = git('rev-parse', 'HEAD')
    snapshots = []
    for number in range(1, 6):
        (source / 'value.py').write_text(f'value = {number}\n')
        git('commit', '-am', '修改')
        head = git('rev-parse', 'HEAD')
        enqueue(store, repo, number)
        snapshots.append({'id': head, 'base': base, 'head': head, 'merge_base': base, 'workspace_layout': 'git-worktree-v2'})
    jobs = [store.claim() for _ in snapshots]
    original = git_workspace.run_git
    def local_git(args, **kwargs):
        if 'fetch' in args:
            return subprocess.check_output(['git', '-C', str(kwargs['cwd']), 'fetch', str(source), '+refs/heads/*:refs/remotes/origin/*'], stderr=subprocess.DEVNULL)
        return original(args, **kwargs)
    monkeypatch.setattr(git_workspace, 'run_git', local_git)
    monkeypatch.setattr('ai_review.authorization.authorize', lambda *args: None)
    monkeypatch.setattr('ai_review.gitea.assert_version', lambda *args: None)
    client = SimpleNamespace(url='http://test', repository=lambda: None, check_active=lambda: None)
    full_repo = store.repo(repo['id'])
    def prepare(pair):
        job, snapshot = pair
        workspace, _ = git_workspace.prepare(store, client, full_repo, job, snapshot)
        store.update(job['id'], job['owner'], workspace_path=workspace, snapshot=snapshot)
        return Path(workspace)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        paths = list(pool.map(prepare, zip(jobs, snapshots)))
    assert len(set(paths)) == 5
    for number, path in enumerate(paths, 1):
        assert (path / 'value.py').read_text() == f'value = {number}\n'
        assert git_workspace.read_blob(path, base, 'value.py') == b'value = 0\n'
    from ai_review.git_cache import clear
    with pytest.raises(Exception, match='运行或待回收'):
        clear(store, full_repo)
    store.finish(jobs[0]['id'], jobs[0]['owner'], 'succeeded')
    concurrency.reap(store)
    assert not paths[0].exists()
    assert all(path.exists() for path in paths[1:])


def test_reject_invalid_limits_without_changing_jobs(setup):
    store, repo = setup
    enqueue(store, repo, 1)
    for value in (0, 6, True, 1.5, '5'):
        with pytest.raises(Exception, match='并发数'):
            store.save_config({'revision': store.config()['revision'], 'max_concurrent_reviews': value})
    assert store.config()['max_concurrent_reviews'] == 5
    assert store.claim()


def test_executor_capacity_and_workspace_boundaries(tmp_path, monkeypatch):
    import threading
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'review-runtime'))
    from native_session import NativeSessions, NativeError
    root = tmp_path / 'workspaces'
    monkeypatch.setenv('AI_REVIEW_WORKSPACE_ROOT', str(root))
    monkeypatch.setenv('AI_REVIEW_NATIVE_ROOT', str(tmp_path / 'runtime'))
    job_id = 'a' * 32
    folder = root / '.repositories' / ('b' * 64) / 'worktrees' / job_id
    folder.mkdir(parents=True)
    sessions = NativeSessions()
    data = {'job_id': job_id, 'session_id': 'c' * 32, 'workspace': str(folder), 'prompt': '固定验收',
            'model': '验收模型', 'deadline_at': time.time() + 120, 'timeout': 120,
            'event_url': 'http://test', 'event_token': '测试', 'capability': '测试',
            'engine': 'codex_cli', 'workspace_layout': 'git-worktree-v2'}
    assert sessions._workspace(data) == folder
    with pytest.raises(NativeError, match='workspace_outside_repository'):
        sessions._workspace(dict(data, job_id='d' * 32))
    for i in range(5):
        sessions.sessions[str(i)] = SimpleNamespace(done=threading.Event(), job_id=str(i))
    assert sessions.capacity() == {'limit': 5, 'active': 5}
    with pytest.raises(NativeError, match='executor_capacity'):
        sessions.execute(data)


def test_mixed_repositories_claim_once_across_processes(setup):
    import os
    import sys
    store, repo = setup
    second = store.save_repo(1, 2, {'enabled': True, 'repo_name': 'test/other', 'gitea_url': 'http://test', 'bot_token': store.seal('测试')})
    for number in range(1, 6):
        enqueue(store, repo, number)
        enqueue(store, second, number)
    script = "from ai_review.store import Store; import sys; j=Store(sys.argv[1]).claim(); print(j['id'] if j else '')"
    children = [subprocess.Popen([sys.executable, '-c', script, str(store.path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(8)]
    claimed = []
    for child in children:
        out, error = child.communicate(timeout=30)
        assert child.returncode == 0, error
        if out.strip():
            claimed.append(out.strip())
    assert len(claimed) == len(set(claimed)) == 5
    assert concurrency.status(store)['queued'] == 5


def test_immediate_continuation_keeps_and_adopts_its_slot(setup):
    store, repo = setup
    job_id = enqueue(store, repo, 1)
    old = store.claim()
    with store.tx() as connection:
        connection.execute("UPDATE jobs SET status='queued',owner='',lease=0 WHERE id=?", (job_id,))
    assert store.claim() is None
    concurrency.reap(store)
    current = store.claim()
    assert current['id'] == job_id
    assert current['owner'] != old['owner']
    assert concurrency.status(store)['occupied'] == 1


def test_push_during_expired_lease_recovery_never_resurrects_old_job(setup, monkeypatch):
    store, repo = setup
    ident = enqueue(store, repo, 1)
    store.claim()
    with store.tx() as connection:
        connection.execute('UPDATE jobs SET lease=0 WHERE id=?', (ident,))
    original = store.job
    pushed = False
    def read_then_push(job_id, **kwargs):
        nonlocal pushed
        value = original(job_id, **kwargs)
        if not pushed:
            pushed = True
            store.enqueue(repo['id'], 1, {'head': 'new', 'base': '0'})
        return value
    monkeypatch.setattr(store, 'job', read_then_push)
    concurrency.reap(store)
    assert original(ident)['status'] == 'superseded'
