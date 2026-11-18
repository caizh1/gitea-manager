"""自动发布从真实台账入口验证，不调用模型，覆盖写入与失联恢复。"""
import copy
import json
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from test_ai_review_issue_lifecycle import LifecycleTests, Client
from ai_review.common import PublishUnknown
from ai_review.issue_lifecycle import reconcile
from ai_review.publication import publish, resume
from ai_review.progress import ProgressSync
from ai_review.store import Store
from test_ai_review_progress import decode_body


class AutomaticClient(Client):
    def __init__(self, snapshot):
        super().__init__(snapshot)
        self.summary_failure = False
        self.assert_frozen = None

    def close(self):
        pass

    def request(self, method, path, data=None, **kwargs):
        if method in ('POST', 'PATCH') and self.assert_frozen:
            self.assert_frozen()
        if method == 'PATCH' and '/issues/comments/' in path and not any(
                row['id'] == int(path.split('/')[-1]) for row in self.summaries):
            return super().request(method, path, data, **kwargs)
        if '/issues/' in path and '/comments' in path:
            if method == 'GET':
                return copy.deepcopy(next((row for row in self.summaries if row['id'] == int(path.split('/')[-1])), None))
            if method == 'POST':
                row = {'id': 10000 + len(self.summaries), 'body': data['body'], 'user': {'id': 9},
                       'html_url': 'http://gitea/test/code/pulls/1'}
                self.summaries.append(row)
            else:
                row = next(row for row in self.summaries if row['id'] == int(path.split('/')[-1]))
                row['body'] = data['body']
            self.calls.append((method, path, copy.deepcopy(data)))
            if self.summary_failure:
                self.summary_failure = False
                raise PublishUnknown('总览已写入但回执丢失')
            return copy.deepcopy(row)
        return super().request(method, path, data, **kwargs)


@pytest.fixture
def case():
    value = LifecycleTests()
    value.setUp()
    value.client = AutomaticClient(value.snapshot)
    value.snapshot['engine'] = 'codex_cli'
    value.repo_value['gitea_url'] = 'http://gitea'
    value.store.update(value.job['id'], value.job['owner'], snapshot=value.snapshot)
    with value.store.tx() as c:
        config = json.loads(c.execute('SELECT value FROM configuration_history WHERE revision=?', (value.job['config_revision'],)).fetchone()[0])
        config['engine'] = 'codex_cli'
        config['api_key'] = ''
        c.execute('UPDATE settings SET value=?', (json.dumps(config),))
        repo = json.loads(c.execute('SELECT value FROM repositories WHERE id=?', (value.repo,)).fetchone()[0])
        repo['gitea_url'] = 'http://gitea'
        c.execute('UPDATE repositories SET value=? WHERE id=?', (json.dumps(repo), value.repo))
        c.execute('UPDATE configuration_history SET value=? WHERE revision=?', (json.dumps(config), value.job['config_revision']))
    yield value
    value.doCleanups()


def result(case, findings=True):
    value = case.report()
    if not findings:
        value['findings'] = []
        value['report_markdown'] = 'VERDICT: PASS'
    value['analysis_complete'] = True
    value = reconcile(case.store, case.client, case.job, case.repo_value, case.snapshot, value)
    case.store.update(case.job['id'], case.job['owner'], result=value)
    case.job = case.store.job(case.job['id'])
    return value


def send(case):
    try:
        publish(case.store, case.client, case.job, case.repo_value)
    except PublishUnknown as error:
        # 模拟 Worker 的失败落盘，以及无原生执行器的测试任务占位回收。
        case.store.finish(case.job['id'], case.job['owner'], 'publish_unknown', str(error))
        from ai_review.concurrency import reap
        reap(case.store)
        raise


def retry(case):
    with case.store.tx() as c:
        c.execute("UPDATE jobs SET status='publish_unknown',owner='',lease=0,available=0 WHERE id=?", (case.job['id'],))
    from ai_review.concurrency import reap
    reap(case.store)
    with patch('ai_review.worker.client_for', return_value=case.client), patch('ai_review.worker.refresh_pr'):
        resume(case.store, case.store.job(case.job['id']))


def test_frozen_plan_and_automatic_inline_publication(case):
    result(case)
    def frozen():
        with case.store.connect() as c:
            assert c.execute('SELECT 1 FROM review_publication_plans').fetchone()
            assert c.execute("SELECT 1 FROM review_issue_operations WHERE kind='summary'").fetchone()
            assert c.execute('SELECT count(*) FROM review_publication_approvals').fetchone()[0] == 0
    case.client.assert_frozen = frozen
    send(case)
    assert case.store.job(case.job['id'])['status'] == 'succeeded'
    assert len(case.client.code[1]) == 1
    assert len(case.client.summaries) == 1
    assert case.store.job(case.job['id'])['publication_status']['comments_published'] == 1


def test_pass_creates_only_automatic_summary(case):
    result(case, False)
    send(case)
    assert not case.client.reviews
    assert 'VERDICT: PASS' in case.client.summaries[0]['body']
    assert case.store.job(case.job['id'])['status'] == 'succeeded'


@pytest.mark.parametrize('findings', [True, False])
def test_completed_codex_summary_gains_progress_without_republishing(case, findings):
    result(case, findings)
    send(case)
    summary = copy.deepcopy(case.client.summaries[0])
    reviews = copy.deepcopy(case.client.reviews)
    task = case.store.job(case.job['id'])
    sync = ProgressSync(case.store, lambda _: case.client)
    with patch.object(case.client, 'repository', create=True):
        assert sync.sync(case.repo, 1)
        payload = decode_body(case.client.summaries[0])
        assert payload['status'] == 'succeeded'
        assert payload['head'] == case.snapshot['head']
        assert summary['body'].split('\n', 1)[1] in case.client.summaries[0]['body']
        assert case.client.summaries[0]['id'] == summary['id']
        assert len(case.client.summaries) == 1
        assert case.client.reviews == reviews
        with case.store.tx() as connection:
            connection.execute('UPDATE pr_state SET progress_next=0,progress_synced=progress_seq')
            connection.execute("UPDATE jobs SET session_id='' WHERE id=?", (case.job['id'],))
        sync.refresh_native()
        assert len(sync.pending()) == 1
        assert sync.sync(case.repo, 1)
    updated = case.store.job(case.job['id'])
    for key in ('status', 'result', 'model_calls', 'generation'):
        assert updated[key] == task[key]
    assert case.client.reviews == reviews
    assert len(case.client.summaries) == 1


def test_codex_progress_preserves_pending_publication_receipt(case):
    result(case)
    case.client.summary_failure = True
    with pytest.raises(PublishUnknown):
        send(case)
    summary = copy.deepcopy(case.client.summaries)
    sync = ProgressSync(case.store, lambda _: case.client)
    with patch.object(case.client, 'repository', create=True):
        assert not sync.sync(case.repo, 1)
    assert case.client.summaries == summary
    retry(case)
    assert case.store.job(case.job['id'])['status'] == 'succeeded'
    assert len(case.client.summaries) == 1


def test_codex_progress_waits_for_publisher_and_preserves_human_edits(case):
    result(case)
    send(case)
    summary = copy.deepcopy(case.client.summaries)
    sync = ProgressSync(case.store, lambda _: case.client)
    with patch.object(case.client, 'repository', create=True):
        with case.store.lock(f'进度-{case.repo}-1'):
            assert not sync.sync(case.repo, 1)
        assert case.client.summaries == summary
        with case.store.tx() as connection:
            connection.execute('UPDATE pr_state SET progress_next=0')
        case.client.summaries[0]['body'] += '\n人工补充说明'
        assert not sync.sync(case.repo, 1)
    assert case.client.summaries[0]['body'].endswith('人工补充说明')


def test_codex_publication_waits_for_progress_update(case):
    result(case)
    entered = threading.Event()
    lock = case.store.lock

    @contextmanager
    def observed_lock(name, **kwargs):
        if name == f'进度-{case.repo}-1':
            entered.set()
        with lock(name, **kwargs):
            yield

    with ThreadPoolExecutor(max_workers=1) as pool:
        with patch.object(case.store, 'lock', side_effect=observed_lock):
            with lock(f'进度-{case.repo}-1'):
                future = pool.submit(send, case)
                assert entered.wait(2)
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.05)
            future.result(timeout=5)
    assert case.store.job(case.job['id'])['status'] == 'succeeded'
    assert len(case.client.summaries) == 1


def test_next_codex_publication_reuses_summary_after_progress_sync(case):
    result(case)
    send(case)
    sync = ProgressSync(case.store, lambda _: case.client)
    with patch.object(case.client, 'repository', create=True):
        assert sync.sync(case.repo, 1)
    summary_id = case.client.summaries[0]['id']
    next_version(case, False, 'fixed')
    send(case)
    with case.store.tx() as connection:
        connection.execute('UPDATE pr_state SET progress_next=0')
    with patch.object(case.client, 'repository', create=True):
        assert sync.sync(case.repo, 1)
    assert case.client.summaries[0]['id'] == summary_id
    assert len(case.client.summaries) == 1
    assert decode_body(case.client.summaries[0])['head'] == case.snapshot['head']
    assert '已修复' in case.client.summaries[0]['body']


def test_codex_progress_recovers_lost_patch_receipt_once(case):
    result(case)
    send(case)
    case.client.summary_failure = True
    sync = ProgressSync(case.store, lambda _: case.client)
    with patch.object(case.client, 'repository', create=True):
        assert not sync.sync(case.repo, 1)
        with case.store.tx() as connection:
            connection.execute('UPDATE pr_state SET progress_next=0')
        assert sync.sync(case.repo, 1)
    assert decode_body(case.client.summaries[0])['status'] == 'succeeded'
    assert len(case.client.summaries) == len(case.client.reviews) == 1


def test_summary_lost_receipt_is_recovered_without_duplicate(case):
    result(case)
    case.client.summary_failure = True
    with pytest.raises(PublishUnknown):
        send(case)
    retry(case)
    assert case.store.job(case.job['id'])['status'] == 'succeeded'
    assert len(case.client.reviews) == len(case.client.summaries) == 1


def test_create_lost_receipt_resumes_remaining_summary(case):
    result(case)
    case.client.fail_after = 'COMMENT'
    with pytest.raises(PublishUnknown):
        send(case)
    case.client.fail_after = ''
    retry(case)
    assert len(case.client.reviews) == len(case.client.summaries) == 1
    assert case.store.job(case.job['id'])['status'] == 'succeeded'


def test_legacy_waiting_job_is_requeued_once(case):
    result(case)
    with case.store.tx() as c:
        c.execute("UPDATE jobs SET status='awaiting_publication',owner='',lease=0 WHERE id=?", (case.job['id'],))
        c.execute('PRAGMA user_version=19')
    store = Store(case.store.path)
    assert store.job(case.job['id'])['status'] == 'queued'
    assert Store(case.store.path).job(case.job['id'])['status'] == 'queued'


def test_incomplete_result_never_creates_comment(case):
    result(case)
    case.job['result']['analysis_complete'] = False
    with pytest.raises(Exception, match='完整结束'):
        send(case)
    assert not case.client.reviews and not case.client.summaries


def next_version(case, findings=True, state='present'):
    case.begin('int f(void) { return 2; }\n')
    case.snapshot['engine'] = 'codex_cli'
    case.store.update(case.job['id'], case.job['owner'], snapshot=case.snapshot)
    value = case.report()
    value['analysis_complete'] = True
    if not findings:
        value['findings'] = []
    with patch('ai_review.broker.issue_completion', case.answer(state)):
        value = reconcile(case.store, case.client, case.job, case.repo_value, case.snapshot, value)
    case.store.update(case.job['id'], case.job['owner'], result=value)
    case.job = case.store.job(case.job['id'])


def test_repaired_and_reappeared_issue_keeps_original_comment(case):
    result(case)
    send(case)
    ident = case.job['result']['findings'][0]['issue_id']
    next_version(case, False, 'fixed')
    send(case)
    assert '已修复' in case.client.code[1][0]['body']
    next_version(case)
    send(case)
    assert len(case.client.reviews) == 1
    assert ident == case.job['result']['findings'][0]['issue_id']
    assert '仍存在' in case.client.code[1][0]['body']


def test_human_edited_comment_kept_and_state_goes_to_summary(case):
    result(case)
    send(case)
    case.client.code[1][0]['body'] = '开发者补充的原讨论'
    next_version(case, False, 'fixed')
    send(case)
    assert case.client.code[1][0]['body'] == '开发者补充的原讨论'
    assert '已修复' in case.client.summaries[0]['body']
    assert case.store.job(case.job['id'])['status'] == 'succeeded'
    assert case.store.job(case.job['id'])['publication_status']['errors']


def test_human_edited_summary_is_preserved_and_replaced_once(case):
    result(case)
    send(case)
    case.client.summaries[0]['body'] = '开发者编辑的总览'
    next_version(case, False, 'fixed')
    send(case)
    assert len(case.client.summaries) == 2
    assert case.client.summaries[0]['body'] == '开发者编辑的总览'
    assert '已修复' in case.client.summaries[1]['body']


def test_unknown_receipt_allows_new_codex_analysis(case):
    result(case)
    with case.store.tx() as c:
        c.execute("UPDATE jobs SET status='publish_unknown',owner='',lease=0 WHERE id=?", (case.job['id'],))
    new = case.store.enqueue(case.repo, 1, {'head': 'f'*40, 'base': 'b'*40})
    from ai_review.concurrency import reap
    reap(case.store)
    assert case.store.claim()['id'] == new


def test_unknown_receipt_alert_is_deduplicated(case):
    result(case)
    case.client.fail_after = 'COMMENT'
    with pytest.raises(PublishUnknown):
        send(case)
    case.client.reviews.clear()
    with case.store.tx() as c:
        c.execute('UPDATE review_publication_plans SET created=0 WHERE job=?', (case.job['id'],))
    retry(case)
    retry(case)
    with case.store.connect() as c:
        assert c.execute('SELECT count(*) FROM review_publication_alerts').fetchone()[0] == 1
    assert case.store.job(case.job['id'])['status'] == 'publish_unknown'
    assert case.store.job(case.job['id'])['publication_status']['alert']


def test_new_version_waits_for_old_comment_receipt_then_updates_it(case):
    result(case)
    old_id = case.job['id']
    case.client.fail_after = 'COMMENT'
    with pytest.raises(PublishUnknown):
        send(case)
    old_reviews = case.client.reviews
    case.client.reviews = []
    with case.store.tx() as c:
        c.execute("UPDATE jobs SET status='publish_unknown',owner='',lease=0,available=0 WHERE id=?", (old_id,))
    next_version(case, False, 'fixed')
    with pytest.raises(PublishUnknown):
        send(case)
    assert case.store.job(case.job['id'])['status'] == 'publish_unknown'
    case.client.reviews = old_reviews
    with patch('ai_review.worker.client_for', return_value=case.client), patch('ai_review.worker.refresh_pr'):
        resume(case.store, case.store.job(old_id))
    retry(case)
    assert case.store.job(case.job['id'])['status'] == 'succeeded'
    assert len(case.client.reviews) == 1
    assert '已修复' in case.client.code[1][0]['body']


def test_delayed_patch_receipt_keeps_newer_state_pending(case):
    result(case)
    send(case)
    original = case.client.code[1][0]['body']
    next_version(case, False, 'fixed')
    old_id = case.job['id']
    case.client.fail_after = 'edit'
    with pytest.raises(PublishUnknown):
        send(case)
    repaired = case.client.code[1][0]['body']
    case.client.code[1][0]['body'] = original
    with case.store.tx() as c:
        c.execute("UPDATE jobs SET status='publish_unknown',owner='',lease=0,available=0 WHERE id=?", (old_id,))
    next_version(case, True, 'present')
    with pytest.raises(PublishUnknown):
        send(case)
    with case.store.connect() as c:
        state = c.execute("SELECT state FROM review_issue_operations WHERE job=? AND kind='edit'", (case.job['id'],)).fetchone()[0]
    assert state == 'queued'
    case.client.code[1][0]['body'] = repaired
    retry(case)
    assert case.store.job(case.job['id'])['status'] == 'succeeded'
    assert '仍存在' in case.client.code[1][0]['body']
    assert len(case.client.reviews) == 1


def test_history_confirmed_p1_overrides_primary_pass(case):
    result(case)
    send(case)
    next_version(case, False, 'present')
    assert case.job['result']['report_markdown'].startswith('VERDICT: FAIL')
    send(case)
    assert 'VERDICT: FAIL' in case.client.summaries[0]['body']
    assert '仍存在' in case.client.code[1][0]['body']
    assert len(case.client.reviews) == 1
