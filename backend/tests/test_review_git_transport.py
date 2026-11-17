"""真实 Git HTTP 认证、noexec 凭据助手及失败诊断回归。"""
import base64
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from ai_review.git_cache import GitFailure, git_credentials, run_git


class GitTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='review-git-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.secret = 'fixture-only-git-credential'
        self.source = self.root / 'source'
        self.source.mkdir()
        self.git('init', '-b', 'custom-main')
        self.git('config', 'user.name', '测试')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.source / 'a.py').write_text('a=1\n')
        self.git('add', '.')
        self.git('commit', '-m', '基线')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.head = self.commit(2)
        self.remote = self.root / 'http/owner/repo.git'
        self.remote.parent.mkdir(parents=True)
        subprocess.run(['git', 'clone', '--bare', str(self.source), str(self.remote)],
                       check=True, capture_output=True)
        self.export()
        self.requests = []
        expected = 'Basic ' + base64.b64encode(('oauth2:' + self.secret).encode()).decode()
        requests = self.requests

        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.headers.get('Authorization') == expected)
                if self.headers.get('Authorization') != expected:
                    self.send_response(401)
                    self.send_header('WWW-Authenticate', 'Basic realm="review-fixture"')
                    self.end_headers()
                    return
                super().do_GET()

            def log_message(self, *_args):
                pass

        handler = functools.partial(Handler, directory=str(self.root / 'http'))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.source), *args],
                                       stderr=subprocess.DEVNULL).decode()

    def commit(self, value):
        (self.source / 'a.py').write_text('a=' + str(value) + '\n')
        self.git('commit', '-am', '修改')
        return self.git('rev-parse', 'HEAD').strip()

    def export(self):
        subprocess.run(['git', '-C', str(self.remote), 'fetch', str(self.source),
                        '+refs/heads/*:refs/heads/*'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.remote), 'update-ref', 'refs/pull/1/head', self.head],
                       check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.remote), 'update-server-info'],
                       check=True, capture_output=True)

    def test_interpreter_helper_fetches_without_executable_permission(self):
        destination = self.root / 'checkout.git'
        destination.mkdir()
        run_git(['init', '--bare', '.'], cwd=destination)
        with git_credentials(self.secret) as env:
            helper = Path(shlex.split(env['REVIEW_GIT_CREDENTIAL_HELPER'][1:])[1])
            self.assertEqual(helper.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(self.secret, helper.read_text())
            run_git(['fetch', self.url + '/owner/repo.git', 'refs/heads/custom-main'],
                    cwd=destination, env=env)
        self.assertFalse(helper.exists())
        self.assertIn(True, self.requests)
        self.assertEqual(run_git(['rev-parse', 'FETCH_HEAD'], cwd=destination).decode().strip(), self.head)

    def test_wrong_credential_has_safe_visible_diagnostic(self):
        from ai_review.worker import _failure_diagnostic

        destination = self.root / 'wrong.git'
        destination.mkdir()
        run_git(['init', '--bare', '.'], cwd=destination)
        with git_credentials('wrong-private-fixture') as env:
            with self.assertRaises(GitFailure) as caught:
                run_git(['fetch', self.url + '/owner/repo.git', 'refs/heads/custom-main'],
                        cwd=destination, env=env)
        error = caught.exception
        diagnostic, message = _failure_diagnostic({'stage': '准备 Git 增量缓存'}, error, {'id': 'test'})
        self.assertEqual(diagnostic['phase'], 'fetch')
        self.assertEqual(diagnostic['exit_code'], 128)
        self.assertEqual(diagnostic['reason'], 'authentication')
        self.assertIn('认证失败', message)
        self.assertIn('Authentication failed', diagnostic['git_error'])
        self.assertNotIn('wrong-private-fixture', json.dumps(diagnostic) + message)
        self.assertNotIn(self.url, json.dumps(diagnostic) + message)

    def test_error_classification_keeps_actionable_details(self):
        for stderr, reason in (
            (b"fatal: cannot exec '/tmp/askpass.py': Permission denied", 'credential_helper'),
            (b'fatal: SSL certificate problem: private.example', 'tls_certificate'),
            (b'fatal: Could not resolve host: private.example', 'dns'),
            (b'fatal: detected dubious ownership in repository', 'unsafe_directory'),
            (b"fatal: couldn't find remote ref refs/pull/1/head", 'missing_ref'),
        ):
            with self.subTest(reason=reason):
                error = GitFailure('fetch', 128, stderr)
                self.assertEqual(error.diagnostic['reason'], reason)
                self.assertEqual(error.diagnostic['git_error'], stderr.decode())

    def test_unknown_error_is_preserved_with_credential_redaction(self):
        stderr = b'fatal: unusual failure for https://user:private@gitea.invalid/owner/repo\nprivate-value\n'
        error = GitFailure('fetch', 128, stderr, ['private-value'])
        from ai_review.diagnostics import safe_remote
        detail = safe_remote(error.diagnostic)['git_error']
        self.assertIn('unusual failure', detail)
        self.assertNotIn('private', detail)
        self.assertNotIn('gitea.invalid', detail)

    def test_native_workspace_first_fetch_reuse_and_incremental_update(self):
        from ai_review import git_workspace, native_workspace
        from ai_review.store import Store

        key = self.root / 'master.key'
        key.write_bytes(Fernet.generate_key())
        with patch.dict(os.environ, {'AI_REVIEW_MASTER_KEY_FILE': str(key)}), \
                patch.object(native_workspace, 'WORKSPACE_ROOT', self.root / 'workspaces'), \
                patch('ai_review.authorization.authorize'), patch('ai_review.gitea.assert_version'):
            store = Store(self.root / 'review.db')
            config = store.config()
            config.update(enabled=True, debounce_seconds=0)
            store.save_config(config)
            repo = store.save_repo(1, 1, {'enabled': True, 'gitea_url': self.url,
                                         'repo_name': 'owner/repo', 'bot_token': store.seal(self.secret)})
            repo = store.repo(repo['id'])
            client = SimpleNamespace(url=self.url, repository=lambda: None, check_active=lambda: None)
            modes = []
            for step in range(3):
                if step == 2:
                    self.head = self.commit(3)
                    self.export()
                job_id = store.enqueue(repo['id'], 1, {'base': self.base, 'head': self.head}, manual=True)
                job = store.claim()
                snapshot = {'id': self.head, 'base': self.base, 'head': self.head,
                            'merge_base': self.base, 'diff': self.git('diff', self.base, self.head)}
                workspace, info = git_workspace.prepare(store, client, repo, job, snapshot)
                modes.append(info['mode'])
                self.assertEqual((Path(workspace) / 'a.py').read_text(), 'a=' + str(3 if step == 2 else 2) + '\n')
                store.finish(job_id, job['owner'], 'succeeded')
            self.assertEqual(modes, ['首次获取', '缓存复用', '增量更新'])


if __name__ == '__main__':
    unittest.main()
