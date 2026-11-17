"""自动发布从真实台账入口验证，不调用模型，覆盖写入与失联恢复。"""
import copy
import json
from unittest.mock import patch

import pytest
from test_ai_review_issue_lifecycle import LifecycleTests, Client
from ai_review.common import PublishUnknown
from ai_review.issue_lifecycle import reconcile
from ai_review.publication import publish, resume
from ai_review.store import Store


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
    publish(case.store, case.client, case.job, case.repo_value)


def retry(case):
    with case.store.tx() as c:
        c.execute("UPDATE jobs SET status='publish_unknown',owner='',lease=0,available=0 WHERE id=?", (case.job['id'],))
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
