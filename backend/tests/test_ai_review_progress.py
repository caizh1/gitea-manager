"""进度状态、网络故障、代次隔离和离线安装的确定性测试。"""
import base64
import copy
import importlib.util
import json
from pathlib import Path
import time
import unittest
import zipfile
from unittest.mock import patch

import test_ai_review as core
from ai_review.common import PublishUnknown, ReviewError, Stale, WriteRejected
from ai_review.engine import review
from ai_review.progress import ProgressSync, SUMMARY_MARK, DATA_PREFIX, state_for, payload_for
from ai_review.progress_assets import build_assets
from ai_review.store import Store


class CommentGitea:
    prefix = '/repos/test/code'

    def __init__(self):
        self.head = 'a' * 40
        self.state = 'open'
        self.rows = []
        self.writes = []
        self.fail = ''
        self.before_write = None
        self.review = None

    def repository(self):
        if self.fail == 'offline':
            raise ReviewError('Gitea 连接失败或超时')
        return {'id': 7, 'full_name': 'test/code'}

    def close(self):
        pass

    def pr(self, number):
        return {'state': self.state, 'head': {'sha': self.head}, 'base': {'sha': 'b'*40, 'repo': {'id': 7}}, 'number': number}

    def pages(self, path):
        return copy.deepcopy(self.rows)

    def request(self, method, path, data=None, missing=False):
        if self.fail == 'offline':
            raise ReviewError('Gitea 连接失败或超时')
        if method == 'GET':
            if '/reviews/' in path:
                return copy.deepcopy(self.review)
            return copy.deepcopy(next((r for r in self.rows if str(r['id']) == path.split('/')[-1]), None))
        if self.before_write:
            f = self.before_write; self.before_write = None; f()
        self.writes.append(method)
        if self.fail == 'rejected':
            self.fail = ''; raise WriteRejected('Gitea 明确拒绝写入：HTTP 403')
        if self.fail == 'lost_without_write':
            self.fail = ''; raise PublishUnknown('请求状态未知')
        row = {'id': 501, 'user': {'id': 9}, 'body': data['body'], 'pull_request_url': 'http://gitea/test/code/pulls/1'}
        self.rows[:] = [row]
        if self.fail == 'lost':
            self.fail = ''; raise PublishUnknown('响应丢失')
        return copy.deepcopy(row)


def decode_body(row):
    return json.loads(base64.urlsafe_b64decode(row['body'].split(DATA_PREFIX)[1].split(' -->')[0]))


class ProgressTests(unittest.TestCase):
    def setUp(self):
        core.StoreTests.setUp(self)
        self.store.repo_status(self.repo, gitea_url='http://gitea')
        self.id = self.store.enqueue(self.repo, 1, {'head': 'a'*40, 'base': 'b'*40})
        with self.store.tx() as c:
            c.execute('UPDATE jobs SET batch_protocol=0 WHERE id=?', (self.id,))
        self.client = CommentGitea()
        self.sync = ProgressSync(self.store, lambda _: self.client)

    def ready(self):
        with self.store.tx() as c:
            c.execute('UPDATE pr_state SET progress_next=0')

    def send(self):
        self.ready()
        return self.sync.sync(self.repo, 1)

    def test_received_sha_and_no_invented_start(self):
        value = self.store.job(self.id)['progress']
        self.assertEqual(value['head'], 'a'*40)
        self.assertIsNone(value['started_at'])
        self.assertIsNone(value['units_total'])
        self.assertTrue(self.send())
        payload = decode_body(self.client.rows[0])
        self.assertEqual(payload['status'], 'queued')
        self.assertNotIn('bot_token', payload)
        self.assertEqual(self.client.writes, ['POST'])

    def test_superseded_list_matches_detail_without_rewriting_stored_stage(self):
        self.store.enqueue(self.repo, 1, {'head': 'c' * 40, 'base': 'b' * 40})
        detail = self.store.job(self.id, public=True)
        row = next(item for item in self.store.jobs()['items'] if item['id'] == self.id)
        self.assertEqual(row['status'], 'superseded')
        self.assertEqual(row['stage'], detail['stage'])
        self.assertEqual(row['stage'], '版本已过期')
        self.assertEqual(row['progress']['detail'], detail['progress']['detail'])
        with self.store.connect() as connection:
            raw = connection.execute('SELECT stage FROM jobs WHERE id=?', (self.id,)).fetchone()[0]
        self.assertEqual(raw, '等待执行')

    def test_live_stage_is_preserved_in_list_and_detail(self):
        job = self.store.claim()
        self.store.update(self.id, job['owner'], stage='正在准备审查材料')
        row = next(item for item in self.store.jobs()['items'] if item['id'] == self.id)
        self.assertEqual(row['stage'], '正在准备审查材料')
        self.assertEqual(row['stage'], self.store.job(self.id)['stage'])

    def test_archived_native_job_does_not_display_its_old_running_stage(self):
        job = self.store.claim()
        self.store.update(self.id, job['owner'], stage='正在读取代码')
        with self.store.tx() as connection:
            connection.execute('UPDATE jobs SET session_id=? WHERE id=?', ('session_archived_test', self.id))
        self.store.archive_repo(self.repo)
        row = next(item for item in self.store.jobs()['items'] if item['id'] == self.id)
        self.assertEqual(row['stage'], '版本已过期')
        self.assertEqual(row['progress']['label'], '版本已过期')

    def test_native_progress_and_restart_refresh_do_not_repeat_review(self):
        job = self.store.claim()
        with self.store.tx() as c:
            c.execute('UPDATE jobs SET session_id=? WHERE id=?', ('session_progress_test', self.id))
        self.assertTrue(self.send())
        payload = decode_body(self.client.rows[0])
        self.assertEqual(payload['units_completed'], 0)
        self.assertIsNone(payload['units_total'])
        self.assertIsNone(self.store.job(self.id)['progress']['units_completed'])
        self.assertNotIn('完成分析单元', self.client.rows[0]['body'])
        self.store.finish(job['id'], job['owner'], 'succeeded')
        self.assertTrue(self.send())
        before = self.store.job(self.id)
        self.ready()
        self.assertEqual(self.sync.pending(), [])
        self.sync.refresh_native()
        self.assertEqual(len(self.sync.pending()), 1)
        self.assertTrue(self.send())
        self.assertEqual(decode_body(self.client.rows[0])['status'], 'succeeded')
        self.assertEqual(self.client.writes.count('POST'), 1)
        after = self.store.job(self.id)
        for key in ('id', 'status', 'generation', 'model_calls', 'result'):
            self.assertEqual(before[key], after[key])
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)

    def test_lifecycle_and_read_only_progress(self):
        self.send(); job = self.store.claim()
        self.store.update(job['id'], job['owner'], progress_stage='refutation', stage='反证检查', units_total=3, units_completed=1)
        self.assertTrue(self.send())
        payload = decode_body(self.client.rows[0])
        self.assertEqual((payload['stage'], payload['units_completed'], payload['units_total']), ('refutation', 1, 3))
        self.assertIsNotNone(payload['started_at'])
        self.store.finish(job['id'], job['owner'], 'failed', '模型输出无效')
        self.assertTrue(self.send())
        payload = decode_body(self.client.rows[0])
        self.assertEqual(payload['status'], 'failed')
        self.assertIsNotNone(payload['finished_at'])
        self.assertEqual(self.client.writes, ['POST', 'PATCH', 'PATCH'])

    def test_rollback_has_no_dirty_signal(self):
        state, _ = state_for(self.store, self.repo, 1)
        try:
            with self.store.tx() as c:
                c.execute("UPDATE jobs SET stage='未提交的阶段' WHERE id=?", (self.id,))
                raise RuntimeError('事务取消')
        except RuntimeError:
            pass
        self.assertEqual(state_for(self.store, self.repo, 1)[0]['progress_seq'], state['progress_seq'])

    def test_expired_worker_is_not_reported_running(self):
        job = self.store.claim()
        with self.store.tx() as c:
            c.execute('UPDATE jobs SET lease=0 WHERE id=?', (job['id'],))
        self.send()
        self.assertEqual(decode_body(self.client.rows[0])['status'], 'interrupted')
        self.assertEqual(self.store.job(job['id'])['status'], 'running')

    def test_expired_publisher_requires_reconciliation(self):
        job = self.store.claim(); self.store.update(job['id'], job['owner'], status='publishing')
        with self.store.tx() as c:
            c.execute('UPDATE jobs SET lease=0 WHERE id=?', (job['id'],))
        self.send()
        self.assertEqual(decode_body(self.client.rows[0])['status'], 'publish_unknown')

    def test_lost_create_response_is_adopted_without_duplicate(self):
        self.client.fail = 'lost'
        self.assertFalse(self.send())
        self.assertTrue(self.send())
        self.assertEqual(self.client.writes.count('POST'), 1)
        state, _ = state_for(self.store, self.repo, 1)
        self.assertEqual(state['summary_id'], 501)
        self.assertEqual(state['progress_creating'], 0)

    def test_absent_uncertain_create_is_not_blindly_retried(self):
        self.client.fail = 'lost_without_write'
        self.assertFalse(self.send()); self.assertFalse(self.send())
        self.assertEqual(self.client.writes, ['POST'])
        self.assertIn('待核对', state_for(self.store, self.repo, 1)[0]['progress_error'])

    def test_explicit_rejection_can_retry_after_permissions_repaired(self):
        self.client.fail = 'rejected'
        self.assertFalse(self.send())
        self.assertEqual(state_for(self.store, self.repo, 1)[0]['progress_creating'], 0)
        self.assertTrue(self.send())

    def test_pruned_results_do_not_claim_zero_findings(self):
        job = self.store.claim(); self.store.finish(job['id'], job['owner'], 'succeeded')
        self.send(); self.assertIsNone(decode_body(self.client.rows[0])['findings'])

    def test_progress_outage_does_not_change_review_success(self):
        job = self.store.claim(); self.store.finish(job['id'], job['owner'], 'succeeded')
        self.client.fail = 'offline'; self.assertFalse(self.send())
        self.assertEqual(self.store.job(self.id)['status'], 'succeeded')
        self.client.fail = ''; self.assertTrue(self.send())

    def test_new_generation_during_write_remains_dirty(self):
        self.client.before_write = lambda: self.store.enqueue(self.repo, 1, {'head': 'c'*40})
        self.send()
        state, _ = state_for(self.store, self.repo, 1)
        self.assertGreater(state['progress_seq'], state['progress_synced'])
        self.client.head = 'c'*40; self.send()
        self.assertEqual(decode_body(self.client.rows[0])['head'], 'c'*40)
        self.assertEqual(self.client.writes.count('POST'), 1)

    def test_stale_model_cannot_override_new_queue(self):
        job = self.store.claim(); self.store.enqueue(self.repo, 1, {'head': 'c'*40}); self.client.head = 'c'*40
        with self.assertRaises(Stale): self.store.update(self.id, job['owner'], stage='迟到结果')
        self.send(); self.assertEqual(decode_body(self.client.rows[0])['status'], 'queued')

    def test_closed_and_disabled_pr(self):
        self.store.enqueue(self.repo, 1, {'head': 'a'*40}, closed=True)
        self.send(); self.assertEqual(decode_body(self.client.rows[0])['status'], 'stopped')
        self.store.enqueue(self.repo, 1, {'head': 'a'*40})
        self.store.save_config({'revision': self.store.config()['revision'], 'enabled': False})
        self.send(); self.assertFalse(decode_body(self.client.rows[0])['enabled'])

    def test_missing_webhook_detects_version_mismatch(self):
        self.client.head = 'd'*40
        self.send(); self.assertEqual(decode_body(self.client.rows[0])['status'], 'outdated')

    def test_wrong_author_and_wrong_pr_preserve_discussion(self):
        self.send(); self.client.rows[0]['user']['id'] = 10
        self.assertFalse(self.send()); self.assertEqual(len(self.client.writes), 1)
        self.client.rows[0]['user']['id'] = 9; self.client.rows[0]['pull_request_url'] = 'http://gitea/test/code/pulls/2'
        self.assertFalse(self.send()); self.assertEqual(len(self.client.writes), 1)

    def test_unverifiable_legacy_summary_is_preserved(self):
        self.client.rows = [{'id': 501, 'user': {'id': 9}, 'body': SUMMARY_MARK + '\n历史总体状态', 'pull_request_url': 'http://gitea/test/code/pulls/1'}]
        self.assertFalse(self.send()); self.assertEqual(self.client.writes, [])

    def test_untouched_legacy_summary_is_adopted(self):
        self.assertTrue(self.send())
        with self.store.tx() as c:
            c.execute('DELETE FROM review_issue_summaries')
            c.execute('UPDATE pr_state SET summary_id=NULL')
        self.assertTrue(self.send())
        self.assertEqual(self.client.writes.count('POST'), 1)

    def test_manual_summary_edit_and_deletion_are_preserved(self):
        self.assertTrue(self.send())
        self.client.rows[0]['body'] += '\n人工备注'
        self.assertFalse(self.send())
        self.assertIn('人工备注', self.client.rows[0]['body'])
        self.client.rows.clear()
        self.assertFalse(self.send())
        self.assertEqual(self.client.writes, ['POST'])

    def test_legacy_reference_uses_verified_review_not_issue_number(self):
        job = self.store.claim(); self.store.update(job['id'], job['owner'], review_id=42)
        self.store.finish(job['id'], job['owner'], 'succeeded')
        self.client.review = {'id': 42, 'state': 'COMMENT', 'user': {'id': 9}, 'commit_id': 'a'*40,
                              'body': '<!-- ai-review:v1:job:'+job['id']+' -->',
                              'html_url': 'http://gitea/test/code/pulls/1#issuecomment-73'}
        self.assertTrue(self.send())
        self.assertEqual(decode_body(self.client.rows[0])['review_fragment'], 'issuecomment-73')
        self.assertIn('[AI Review 42](#issuecomment-73)', self.client.rows[0]['body'])

    def test_legacy_reference_rejects_other_version_or_owner(self):
        job = self.store.claim(); self.store.update(job['id'], job['owner'], review_id=42)
        self.store.finish(job['id'], job['owner'], 'succeeded')
        self.client.review = {'id': 42, 'state': 'COMMENT', 'user': {'id': 10}, 'commit_id': 'b'*40,
                              'body': '<!-- ai-review:v1:job:'+job['id']+' -->',
                              'html_url': 'http://gitea/test/code/pulls/2#issuecomment-73'}
        self.assertTrue(self.send())
        self.assertEqual(decode_body(self.client.rows[0])['review_fragment'], '')
        self.assertNotIn('#42', self.client.rows[0]['body'])

    def test_stage_rate_limit_and_heartbeat_coalescing(self):
        self.send(); job = self.store.claim(); self.store.update(self.id, job['owner'], stage='分析中')
        self.assertEqual(self.sync.pending(), [])
        self.ready(); self.assertEqual(len(self.sync.pending()), 1)
        self.send(); seq = state_for(self.store, self.repo, 1)[0]['progress_seq']
        self.store.heartbeat(self.id, job['owner'])
        self.assertEqual(state_for(self.store, self.repo, 1)[0]['progress_seq'], seq)

    def test_batch_budget_continues_all_units(self):
        job = self.store.claim()
        config = dict(self.store.config(), max_calls=2, max_findings=5)
        snapshot = {'coverage': {'skipped': []}}
        item = {'path': 'code.c', 'scope': {'name': 'f'}, 'side': 'new', 'anchors': []}
        result = review(self.store, job, config, snapshot, [item, item, item], caller=lambda *args: {'findings': []})
        self.assertEqual(self.store.job(self.id)['progress']['units_completed'], 3)
        self.assertEqual(self.store.job(self.id)['progress']['units_total'], 3)
        self.assertEqual(self.store.job(self.id)['batch_index'], 2)
        self.assertTrue(result['coverage']['complete'])

    def test_schema_restart_preserves_pending_signal(self):
        before = state_for(self.store, self.repo, 1)[0]
        again = Store(self.store.path)
        self.assertEqual(state_for(again, self.repo, 1)[0]['progress_seq'], before['progress_seq'])

    def test_export_has_no_secrets_and_installer_preserves_custom_footer(self):
        bundle = build_assets([self.store.repo(self.repo)])
        with zipfile.ZipFile(bundle) as archive:
            for name in archive.namelist():
                self.assertNotIn('仅测试凭据'.encode(), archive.read(name))
            fragment = archive.read('footer.fragment.tmpl').decode()
            self.assertIn('{{AppSubUrl}}', fragment)
            self.assertIn('data-review-bots="9"', fragment)
        path = Path(__file__).parents[1] / 'ai_review/assets/install.py'
        spec = importlib.util.spec_from_file_location('安装器', path)
        installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
        original = '<footer>公司原有页脚</footer>\n'
        installed = installer.merged_footer(original, fragment)
        self.assertEqual(installed, installer.merged_footer(installed, fragment))
        self.assertTrue(installer.merged_footer(installed).startswith(original))
        with self.assertRaises(ValueError): installer.merged_footer(installer.BEGIN, fragment)


if __name__ == '__main__':
    unittest.main()
