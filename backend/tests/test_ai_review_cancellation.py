"""人工取消、发布竞争、撤权、执行锁和组织隔离的确定性回归。"""
import concurrent.futures
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from ai_review.cancellation import request_cancel, reconcile_cancel
from ai_review.common import ReviewError, Stale
from ai_review.session_events import create_session
from ai_review.store import Store


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        key = root / 'master.key'; key.write_bytes(Fernet.generate_key())
        env = patch.dict(os.environ, {'AI_REVIEW_MASTER_KEY_FILE': str(key)})
        env.start(); self.addCleanup(env.stop)
        self.store = Store(root / 'review.db')
        self.store.save_config({'revision': 1, 'enabled': True, 'debounce_seconds': 0})
        self.repo = self.store.save_repo(1, 7, {'enabled': True, 'repo_name': 'test/code'})['id']
        self.signature = {'head': 'a'*40, 'base': 'b'*40}
        self.ident = self.store.enqueue(self.repo, 1, self.signature)

    def test_queued_cancel_is_idempotent_and_preserves_version(self):
        self.assertEqual(request_cancel(self.store, self.ident, '测试管理员'), 200)
        self.assertEqual(request_cancel(self.store, self.ident, '另一管理员'), 200)
        row = self.store.job(self.ident)
        self.assertEqual(row['status'], 'cancelled')
        self.assertEqual(row['cancel_requested_by'], '测试管理员')
        self.assertIsNotNone(row['finished_at'])
        self.assertIsNone(self.store.claim())
        self.assertIsNone(self.store.enqueue(self.repo, 1, self.signature))
        config = self.store.config()
        self.store.save_config({'revision': config['revision'], 'model': '另一测试模型'})
        self.assertIsNone(self.store.enqueue(self.repo, 1, self.signature))
        self.assertIsNotNone(self.store.enqueue(self.repo, 1, self.signature, manual=True))

    def test_new_commit_can_enqueue_after_cancel(self):
        request_cancel(self.store, self.ident, '测试管理员')
        next_job = self.store.enqueue(self.repo, 1, dict(self.signature, head='c'*40))
        self.assertEqual(self.store.claim()['id'], next_job)

    def test_running_cancel_revokes_and_cannot_be_overwritten(self):
        job = self.store.claim()
        with self.store.tx() as c:
            c.execute('INSERT INTO capabilities VALUES (?,?,?,?,?,?)', ('hash', self.ident, job['owner'], time.time()+100, 9, '测试模型'))
            c.execute('INSERT INTO review_native_bindings VALUES (?,?,?,?,?,1)', ('hash', self.ident, 'session_test', '测试模型', time.time()+100))
        self.assertEqual(request_cancel(self.store, self.ident, '测试管理员'), 202)
        self.assertEqual(request_cancel(self.store, self.ident, '测试管理员'), 202)
        with self.assertRaises(Stale): self.store.update(self.ident, job['owner'], status='publishing')
        self.store.finish(self.ident, job['owner'], 'succeeded')
        self.assertEqual(self.store.job(self.ident)['status'], 'cancelling')
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM capabilities').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT active FROM review_native_bindings').fetchone()[0], 0)

    def test_cancel_and_publish_are_mutually_exclusive(self):
        request_cancel(self.store, self.ident, '测试管理员')
        for number in range(2, 32):
            ident = self.store.enqueue(self.repo, number, self.signature)
            job = self.store.claim()
            self.assertEqual(job['id'], ident)
            target = job['id']
            def publish():
                try:
                    self.store.update(target, job['owner'], status='publishing')
                    return True
                except Stale:
                    return False
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                cancel = pool.submit(request_cancel, self.store, target, '测试管理员')
                publishing = pool.submit(publish)
                code, published = cancel.result(), publishing.result()
            self.assertEqual((code, published), (409, True) if published else (202, False))
            if published: self.store.finish(target, job['owner'], 'succeeded')
            else: self.assertTrue(reconcile_cancel(self.store, {'id': target, 'session_id': ''}))

    def test_confirmation_requires_receipt_and_released_execution_lock(self):
        job = self.store.claim()
        session, _, _ = create_session(self.store, self.ident, job['owner'], time.time()+600, '/app/review-workspaces/'+self.ident)
        request_cancel(self.store, self.ident, '测试管理员')
        target = {'id': self.ident, 'session_id': session}
        with patch('ai_review.native_review.cancel_session', side_effect=ReviewError('执行器不可达')):
            self.assertFalse(reconcile_cancel(self.store, target))
        self.assertEqual(self.store.job(self.ident)['status'], 'cancelling')
        receipt = {'execution_stopped': True, 'status': 'aborted', 'log_stream_closed': True}
        with patch('ai_review.native_review.cancel_session', return_value=receipt), patch('ai_review.native_workspace.cleanup') as cleanup:
            with self.store.lock('execution-' + self.ident):
                self.assertFalse(reconcile_cancel(self.store, target))
                cleanup.assert_not_called()
            self.assertTrue(reconcile_cancel(self.store, target))
            cleanup.assert_called_once_with(self.ident)
        self.assertEqual(self.store.job(self.ident)['status'], 'cancelled')
        self.assertIsNotNone(self.store.job(self.ident)['finished_at'])

    def test_requeued_native_session_still_requires_stop_confirmation(self):
        job = self.store.claim()
        create_session(self.store, self.ident, job['owner'], time.time()+600, '/app/review-workspaces/'+self.ident)
        with self.store.tx() as c:
            c.execute("UPDATE jobs SET status='queued',owner='',lease=0 WHERE id=?", (self.ident,))
        self.assertEqual(request_cancel(self.store, self.ident, '测试管理员'), 202)
        self.assertEqual(self.store.job(self.ident)['status'], 'cancelling')

    def test_maintenance_does_not_run_cancellation(self):
        from ai_review.cancellation import run
        from threading import Event
        stop = Event()
        with patch('services.upgrade_maintenance.active', return_value=True), patch('ai_review.cancellation.reconcile_cancel') as reconcile:
            stop.wait = lambda _: stop.set()
            run(self.store, stop)
            reconcile.assert_not_called()


class CancellationAccessTests(unittest.TestCase):
    def setUp(self):
        from tests.test_organization_access import OrganizationAccessTest
        self.fixture = OrganizationAccessTest(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        with self.fixture.state.tx() as c:
            c.execute("UPDATE jobs SET status='queued'")

    def test_organization_cancel_and_cross_organization_denial(self):
        f = self.fixture
        response = f.request('/api/ai-review/tasks/'+'a'*32+'/cancel', 'post', {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['task']['status'], 'cancelled')
        self.assertEqual(f.request('/api/ai-review/tasks/'+'b'*32+'/cancel', 'post', {}).status_code, 404)
        self.assertEqual(f.state.job('b'*32)['status'], 'queued')

    def test_csrf_and_missing_task(self):
        f = self.fixture
        response = f.admin.post('/api/ai-review/tasks/'+'a'*32+'/cancel', json={}, base_url='https://localhost')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(f.request('/api/ai-review/tasks/'+'f'*32+'/cancel', 'post', {}, client=f.admin, binding=None).status_code, 404)


class ExecutorCancellationTests(unittest.TestCase):
    def test_registered_session_cancelled_before_thread_start_never_spawns(self):
        import threading
        sys.path.insert(0, str(Path(__file__).parents[2] / 'review-runtime'))
        import native_session as native
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); job = 'job_startup_cancel'; session = 'session_startup_cancel'
            workspace = root / 'workspaces' / job; workspace.mkdir(parents=True)
            entered, released = threading.Event(), threading.Event()
            original = native.SessionRun.run
            def delayed(row):
                entered.set(); released.wait(3); original(row)
            reporter = Mock(); reporter.acked_sequence = 0; reporter.closed = True; reporter.queue = []
            reporter.thread.is_alive.return_value = False; reporter.emit.return_value = 1
            data = {'job_id': job, 'session_id': session, 'workspace': str(workspace), 'prompt': '测试审查',
                    'model': '测试模型', 'deadline_at': time.time()+100, 'timeout': 100,
                    'event_url': 'http://review-worker:5051/internal/sessions/events', 'event_token': '测试令牌', 'capability': '测试授权'}
            with patch.dict(os.environ, {'AI_REVIEW_NATIVE_ROOT': str(root/'native'),
                                        'AI_REVIEW_WORKSPACE_ROOT': str(workspace.parent)}), \
                    patch.object(native, 'EventReporter', return_value=reporter), \
                    patch.object(native.SessionRun, 'run', delayed), patch.object(native.subprocess, 'Popen') as spawn:
                registry = native.NativeSessions(); row, _ = registry.execute(data)
                self.assertTrue(entered.wait(2))
                row.result = {'large': 'x' * (2*1024*1024)}
                receipt = registry.abort(session, job)
                self.assertNotIn('result', receipt, '取消回执不应携带大结果正文')
                released.set(); self.assertTrue(row.done.wait(3))
                self.assertEqual(row.status, 'aborted')
                spawn.assert_not_called()
                self.assertTrue(registry.abort(session, job)['execution_stopped'])

    def test_cancel_before_late_execute_survives_registry_restart(self):
        sys.path.insert(0, str(Path(__file__).parents[2] / 'review-runtime'))
        from native_session import NativeSessions, NativeError
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'AI_REVIEW_NATIVE_ROOT': root}):
            registry = NativeSessions()
            session, job = 'session_cancel_test', 'job_cancel_test'
            self.assertTrue(registry.abort(session, job)['execution_stopped'])
            restarted = NativeSessions()
            self.assertTrue(restarted.abort(session, job)['execution_stopped'])
            data = {'job_id': job, 'session_id': session, 'workspace': '/missing/'+job, 'prompt': '测试审查',
                    'model': '测试模型', 'deadline_at': time.time()+100, 'timeout': 100,
                    'event_url': 'http://review-worker:5051/internal/sessions/events', 'event_token': '测试令牌', 'capability': '测试授权'}
            with self.assertRaises(NativeError) as raised: restarted.execute(data)
            self.assertEqual(raised.exception.code, 'authorization_revoked')
            with self.assertRaises(NativeError): restarted.abort('different_session', job)


class CancellationDownloadTests(unittest.TestCase):
    def test_silent_download_socket_is_interrupted(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from ai_review.gitea import ReadSession
        ready, finish = threading.Event(), threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                self.send_response(200); self.send_header('Content-Length', '100'); self.end_headers()
                ready.set(); finish.wait(5)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True); serving.start()
        session = ReadSession(); session.trust_env = False
        def download():
            try:
                with session.get(f'http://127.0.0.1:{server.server_port}', stream=True, timeout=5) as response:
                    list(response.iter_content(100))
            except Exception:
                pass
        reader = threading.Thread(target=download); reader.start()
        try:
            self.assertTrue(ready.wait(2))
            # 响应登记发生在收到头部之后，等待其可中断连接登记完成。
            deadline = time.monotonic()+2
            while not session.connections and time.monotonic() < deadline: time.sleep(.01)
            session.interrupt(); reader.join(2)
            self.assertFalse(reader.is_alive(), '取消后下载仍阻塞')
        finally:
            finish.set(); reader.join(6); session.close(); server.shutdown(); server.server_close()


if __name__ == '__main__':
    unittest.main()
