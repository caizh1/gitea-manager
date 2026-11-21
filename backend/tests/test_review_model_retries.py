"""模拟网关拒绝，核对任务、执行占位和恢复阶段的事务边界。"""

import time

import pytest

from ai_review import concurrency, model_retries
from ai_review.session_events import create_session
from ai_review.store import Store


@pytest.fixture
def queue(tmp_path):
    store = Store(tmp_path / 'review.db')
    store.save_config(dict(store.config(), enabled=True, debounce_seconds=0, max_concurrent_reviews=1))
    repo = store.save_repo(1, 1, {'enabled': True, 'repo_name': 'owner/repo'})
    version = {'head': 'a' * 40, 'base': 'b' * 40}
    ident = store.enqueue(repo['id'], 1, version)
    return store, repo, version, ident


def declined(store, job, status=429, error='ModelAccountTpmRateLimitExceeded', after=None):
    now = time.time()
    sid, _, _ = create_session(store, job['id'], job['owner'], now + 600, '/tmp/review')
    call = '调用-' + sid
    with store.tx() as c:
        c.execute("INSERT INTO review_native_calls VALUES (?,?,?,?,?,'failed',?,?)",
                  (call, '摘要', job['id'], sid, '摘要', now, now))
    store.update(job['id'], job['owner'], diagnostic={
        'call_id': call, 'session_id': sid, 'source': 'model_upstream',
        'upstream_http_status': status, 'reason': 'upstream_http_error',
        'upstream_error_code': error, 'retry_after_seconds': after,
    })
    return sid


def test_rate_limit_releases_slot_only_after_stop_and_preserves_completed_phase(queue, monkeypatch):
    store, repo, version, ident = queue
    job = store.claim()
    session = declined(store, job)
    start = time.time()
    assert model_retries.defer(store, job, 'history-previous', session, start + 600)
    assert store.job(ident)['progress']['label'] == '等待模型额度'
    assert start + 60 <= store.job(ident)['available'] < start + 65
    assert store.enqueue(repo['id'], 1, version, manual=True) == ident
    second = store.enqueue(repo['id'], 2, version)
    monkeypatch.setattr('ai_review.native_review.cancel_session',
                        lambda *_, session_only: {'execution_stopped': not session_only})
    concurrency.reap(store)
    assert store.claim() is None
    monkeypatch.setattr('ai_review.native_review.cancel_session',
                        lambda *_, session_only: {'execution_stopped': session_only})
    concurrency.reap(store)
    assert store.job(ident)['session_id'] == ''
    other = store.claim()
    assert other['id'] == second
    store.finish(second, other['owner'], 'succeeded')
    concurrency.reap(store)
    with store.tx() as c:
        c.execute('UPDATE jobs SET available=0 WHERE id=?', (ident,))
    resumed = Store(store.path).claim()
    assert resumed['id'] == ident and resumed['owner'] != job['owner']
    assert model_retries.deadline(store, ident, 0) >= start + 599
    with store.connect() as c:
        assert c.execute('SELECT phase,attempt FROM review_model_retries').fetchone()[:] == ('history-previous', 1)


@pytest.mark.parametrize(('http_status', 'code'), [(500, 'do_request_failed'), (429, 'insufficient_quota')])
def test_ambiguous_failures_do_not_replay(queue, http_status, code):
    store, _, _, _ = queue
    job = store.claim()
    session = declined(store, job, http_status, code)
    assert not model_retries.defer(store, job, 'primary', session, time.time() + 600)


def test_retry_after_accepts_seconds_and_http_date():
    assert model_retries.retry_after('83', 0) == 83
    assert model_retries.retry_after('Thu, 01 Jan 1970 00:01:00 GMT', 3) == 57
    assert model_retries.retry_after('格式错误', 0) is None
