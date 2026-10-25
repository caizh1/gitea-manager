"""真实本地 HTTP、SQLite 与权限入口的 Git 来源回归，不冒充平台实机验收。"""
from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from flask import Flask
from models import db
from access.control import install
from tests.access_helpers import init_test_auth, login_platform
from svn_migration.common import MigrationError
from git_migration.transport import Transport, SourceHTTPError
from git_migration.providers import Provider
from git_migration.models import GitMigrationSource, GitMigrationDiscovery, GitMigrationInventory
from git_migration.store import save_source, decrypt, enqueue_discovery, enqueue_inventory, public_source, public_discovery
from git_migration.inventory import Inventory
from git_migration.worker import step, recover_interrupted, heartbeat
from routes.git_migration_routes import git_migration_bp


@contextmanager
def source_server(responder):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def dispatch(self):
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length)
            status, headers, result = responder(self.command, self.path, self.headers, body)
            data = result if isinstance(result, bytes) else json.dumps(result).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Type', 'application/json')
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        do_GET = dispatch
        do_POST = dispatch

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class TransportTests(unittest.TestCase):
    def test_server_reduced_page_size_does_not_truncate(self):
        def respond(_method, path, _headers, _body):
            page = int(parse_qs(urlsplit(path).query)['page'][0])
            return 200, {}, [{'id': page}] if page <= 4 else []
        with source_server(respond) as base:
            http = Transport(base)
            self.addCleanup(http.close)
            self.assertEqual([r['id'] for r in http.collection('/items')], [1, 2, 3, 4])

    def test_repeated_page_is_not_a_complete_snapshot(self):
        with source_server(lambda *_: (200, {}, [{'id': 1}])) as base:
            http = Transport(base)
            self.addCleanup(http.close)
            with self.assertRaisesRegex(MigrationError, '分页重复'):
                http.collection('/items')

    def test_total_detects_missing_page_items(self):
        with source_server(lambda *_: (200, {'X-Total': '2', 'X-Next-Page': ''}, [{'id': 1}])) as base:
            http = Transport(base)
            self.addCleanup(http.close)
            with self.assertRaisesRegex(MigrationError, '总数不一致'):
                http.collection('/items')

    def test_explicit_next_page_is_followed(self):
        paths = []
        def respond(_method, path, _headers, _body):
            page = int(parse_qs(urlsplit(path).query)['page'][0])
            paths.append(page)
            return 200, {'X-Next-Page': '3' if page == 1 else '', 'X-Total': '2'}, [{'id': page}]
        with source_server(respond) as base:
            http = Transport(base)
            self.addCleanup(http.close)
            self.assertEqual(len(http.collection('/items')), 2)
            self.assertEqual(paths, [1, 3])

    def test_credentials_refresh_for_each_page(self):
        observed = []
        def respond(_method, path, headers, _body):
            page = int(parse_qs(urlsplit(path).query)['page'][0])
            observed.append(headers.get('Authorization'))
            return 200, {'X-Next-Page': '2' if page == 1 else ''}, [{'id': page}]
        token = iter(['Bearer first', 'Bearer refreshed'])
        with source_server(respond) as base:
            http = Transport(base)
            self.addCleanup(http.close)
            http.collection('/items', credentials=lambda: {'headers': {'Authorization': next(token)}})
        self.assertEqual(observed, ['Bearer first', 'Bearer refreshed'])

    def test_redirect_never_forwards_secret(self):
        observed = []
        with source_server(lambda *args: (observed.append(args) or (200, {}, []))) as target:
            with source_server(lambda *_: (302, {'Location': target + '/stolen'}, {})) as base:
                http = Transport(base)
                self.addCleanup(http.close)
                with self.assertRaisesRegex(MigrationError, '重定向'):
                    http.request('GET', '/items', headers={'Authorization': 'Bearer secret'})
        self.assertEqual(observed, [])

    def test_http_error_never_becomes_empty_or_echoes_body(self):
        with source_server(lambda *_: (403, {}, {'error': 'secret-password'})) as base:
            http = Transport(base)
            self.addCleanup(http.close)
            with self.assertRaises(SourceHTTPError) as context:
                http.collection('/items')
            self.assertNotIn('secret-password', str(context.exception))

    def test_bounded_response(self):
        with source_server(lambda *_: (200, {}, b'[' + b' ' * 300 + b']')) as base:
            http = Transport(base, max_bytes=100)
            self.addCleanup(http.close)
            with self.assertRaisesRegex(MigrationError, '响应过大'):
                http.collection('/items')

    def test_source_mutation_is_rejected(self):
        http = Transport('https://example.test')
        self.addCleanup(http.close)
        with self.assertRaises(MigrationError):
            http.request('POST', '/api/v4/projects', data={})


class ProviderTests(unittest.TestCase):
    def test_gitlab_password_to_bearer_and_namespace(self):
        requests = []
        def respond(method, path, headers, body):
            requests.append((method, path))
            route = urlsplit(path).path
            if route == '/subpath/oauth/token':
                self.assertEqual(parse_qs(body.decode())['password'], ['中文密码'])
                return 200, {}, {'access_token': 'access-test', 'refresh_token': 'refresh-test', 'expires_in': 7200}
            self.assertEqual(headers.get('Authorization'), 'Bearer access-test')
            if route.endswith('/user'):
                return 200, {}, {'id': 1, 'username': 'root', 'is_admin': True, 'state': 'active'}
            if route.endswith('/version'):
                return 200, {}, {'version': '16.1.0-ee', 'enterprise': True}
            if route.endswith('/projects'):
                return 200, {'X-Next-Page': ''}, [{'id': 9, 'name': '工程', 'path_with_namespace': 'org/sub/repo',
                    'http_url_to_repo': 'https://source.test/org/sub/repo.git', 'namespace': {'kind': 'group', 'id': 4}, 'visibility': 'private'}]
            return 200, {'X-Next-Page': ''}, [{'id': 4, 'full_path': 'org/sub', 'name': '子群组', 'parent_id': 3}]
        with source_server(respond) as base:
            http = Transport(base + '/subpath')
            self.addCleanup(http.close)
            result = Provider(http, 'gitlab', 'root', '中文密码').discover()
        self.assertEqual(result['projects'][0]['path'], 'org/sub/repo')
        self.assertFalse(result['migration_verified'])
        self.assertEqual([r for r in requests if r[0] != 'GET'], [('POST', '/subpath/oauth/token')])

    def test_gitlab_disabled_password_requires_token(self):
        with source_server(lambda *_: (400, {}, {'error': 'invalid_grant'})) as base:
            http = Transport(base)
            self.addCleanup(http.close)
            with self.assertRaises(MigrationError) as context:
                Provider(http, 'gitlab', 'root', 'secret').authenticate()
        self.assertEqual(context.exception.code, 'token_required')

    def test_non_admin_cannot_claim_instance_discovery(self):
        with source_server(lambda *_: (200, {}, {'id': 1, 'login': 'user', 'is_admin': False})) as base:
            http = Transport(base)
            self.addCleanup(http.close)
            with self.assertRaisesRegex(MigrationError, '管理员'):
                Provider(http, 'gitea', 'user', 'secret').authenticate()


class SourceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Flask(__name__)
        self.app.secret_key = '本地测试会话密钥' * 5
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///' + self.temp.name + '/test.db', TESTING=True)
        init_test_auth(self.app)
        self.app.register_blueprint(git_migration_bp, url_prefix='/api/git-migrations')
        install(self.app)
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.addCleanup(db.session.remove)
        db.create_all()
        self.client = self.app.test_client()
        self.headers = {'X-CSRF-Token': login_platform(self.client)}

    def source_data(self, **changes):
        return dict({'kind': 'gitlab', 'name': '隔离来源', 'url': 'https://source.test',
                     'username': 'root', 'auth_method': 'password', 'secret': 'source-password'}, **changes)

    def test_secret_is_encrypted_and_not_returned(self):
        row = save_source(self.source_data())
        self.assertEqual(decrypt(row.secret_ciphertext), 'source-password')
        self.assertNotIn('source-password', row.secret_ciphertext)
        self.assertNotIn('source-password', json.dumps(public_source(row)))

    def test_missing_original_key_never_replaced(self):
        row = save_source(self.source_data())
        key = Path(self.temp.name) / 'git-migration.key'
        key.unlink()
        with self.assertRaisesRegex(MigrationError, '原密钥'):
            save_source(self.source_data(version=row.version), row.id)
        self.assertFalse(key.exists())

    def test_replaced_valid_key_cannot_create_mixed_credentials(self):
        from cryptography.fernet import Fernet
        save_source(self.source_data())
        (Path(self.temp.name) / 'git-migration.key').write_bytes(Fernet.generate_key())
        with self.assertRaisesRegex(MigrationError, '密钥损坏'):
            save_source(self.source_data(name='其他来源', url='https://other.test'))
        self.assertEqual(GitMigrationSource.query.count(), 1)

    def test_idempotency_cannot_be_reused_for_different_input(self):
        row = save_source(self.source_data())
        data = {'source_version': row.version, 'request_key': 'test-discover-0001'}
        first = enqueue_discovery(row.id, data, 1)
        self.assertEqual(first.id, enqueue_discovery(row.id, data, 1).id)
        with self.assertRaises(MigrationError):
            enqueue_discovery(row.id, dict(data, source_version=99), 1)

    def test_source_cannot_change_during_discovery(self):
        row = save_source(self.source_data())
        enqueue_discovery(row.id, {'source_version': row.version, 'request_key': 'test-discover-0002'}, 1)
        with self.assertRaisesRegex(MigrationError, '正在发现'):
            save_source(self.source_data(version=row.version), row.id)

    def test_discovery_history_becomes_stale_without_being_overwritten(self):
        row = save_source(self.source_data())
        job = enqueue_discovery(row.id, {'source_version': row.version, 'request_key': 'test-discover-0003'}, 1)
        job.status = 'succeeded'
        job.result_json = '{"证据":"原发现记录"}'
        db.session.commit()
        save_source(self.source_data(version=row.version, name='新名称'), row.id)
        self.assertTrue(public_discovery(job)['stale'])
        self.assertEqual(public_discovery(job)['result']['证据'], '原发现记录')

    def test_restarted_worker_marks_incomplete_results(self):
        row = save_source(self.source_data())
        job = enqueue_discovery(row.id, {'source_version': row.version, 'request_key': 'test-discover-0004'}, 1)
        job.status = 'running'
        db.session.commit()
        recover_interrupted()
        db.session.expire_all()
        self.assertEqual(db.session.get(GitMigrationDiscovery, job.id).status, 'interrupted')

    def test_api_requires_csrf_and_worker(self):
        self.assertEqual(self.client.post('/api/git-migrations/sources', json=self.source_data()).status_code, 403)
        reply = self.client.post('/api/git-migrations/sources', json=self.source_data(), headers=self.headers)
        self.assertEqual(reply.status_code, 201, reply.json)
        row = reply.json
        reply = self.client.post(f'/api/git-migrations/sources/{row["id"]}/discoveries',
                                 json={'source_version': row['version'], 'request_key': 'test-discover-0005'}, headers=self.headers)
        self.assertEqual(reply.status_code, 503)

    def test_worker_preserves_failure_instead_of_success(self):
        row = save_source(self.source_data())
        job = enqueue_discovery(row.id, {'source_version': row.version, 'request_key': 'test-discover-0006'}, 1)
        with patch('git_migration.worker.Provider.discover', side_effect=SourceHTTPError(403)):
            self.assertTrue(step())
        db.session.expire_all()
        result = public_discovery(db.session.get(GitMigrationDiscovery, job.id))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'source_http')
        self.assertEqual(result['result'], {})

    def test_maintenance_does_not_claim_or_write(self):
        row = save_source(self.source_data())
        job = enqueue_discovery(row.id, {'source_version': row.version, 'request_key': 'test-discover-0007'}, 1)
        with patch('git_migration.worker.active', return_value=True):
            self.assertFalse(step())
        self.assertEqual(db.session.get(GitMigrationDiscovery, job.id).status, 'queued')

    def test_maintenance_defers_restart_recovery(self):
        source = save_source(self.source_data())
        job = enqueue_discovery(source.id, {'source_version': source.version, 'request_key': 'restart-maintenance-1'}, 1)
        job.status = 'running'
        db.session.commit()
        with patch('git_migration.worker.active', return_value=True):
            self.assertFalse(recover_interrupted())
        db.session.refresh(job)
        self.assertEqual(job.status, 'running')

    def test_inventory_requires_exact_discovery_membership_and_locks_source(self):
        source = save_source(self.source_data())
        discovery = enqueue_discovery(source.id, {'source_version': source.version, 'request_key': 'inventory-discovery-1'}, 1)
        discovery.status = 'succeeded'
        discovery.result_json = json.dumps({'projects': [{'id': 10, 'path': 'group/repo'}]})
        db.session.commit()
        payload = {'discovery_id': discovery.id, 'project_ids': [11], 'request_key': 'inventory-request-1'}
        with self.assertRaisesRegex(MigrationError, '不属于'):
            enqueue_inventory(source.id, payload, 1)
        payload['project_ids'] = [10]
        job = enqueue_inventory(source.id, payload, 1)
        self.assertEqual(enqueue_inventory(source.id, payload, 1).id, job.id)
        with self.assertRaisesRegex(MigrationError, '正在发现或盘点'):
            save_source(self.source_data(version=source.version), source.id)
        job.status = 'running'
        db.session.commit()
        recover_interrupted()
        db.session.refresh(job)
        self.assertEqual(job.status, 'interrupted')


class InventoryTests(unittest.TestCase):
    def test_legacy_unpaginated_collection_uses_declared_contract(self):
        calls = []
        def respond(_method, path, _headers, _body):
            calls.append(path)
            if path == '/swagger.v1.json':
                return 200, {}, {'paths': {'/repos/{owner}/{repo}/labels': {'get': {'parameters': []}}}}
            self.assertEqual(path, '/api/v1/repos/team/repo/labels')
            return 200, {}, [{'id': 10, 'name': '旧版完整标签列表'}]
        with source_server(respond) as base:
            http = Transport(base)
            self.addCleanup(http.close)
            provider = Provider(http, 'gitea', 'admin', 'fixture-password')
            self.assertEqual(len(provider.collection('/repos/team/repo/labels')), 1)
        self.assertEqual(len(calls), 2)

    def test_missing_endpoint_is_explicit_gap_not_empty_success(self):
        class Provider:
            def collection(self, *args, **kwargs):
                raise SourceHTTPError(403)
        result = Inventory(Provider())
        self.assertEqual(result.collect('私密议题', '/issues'), [])
        self.assertEqual(result.datasets, [])
        self.assertEqual(result.gaps[0]['http_status'], 403)

    def test_expired_authentication_stops_inventory(self):
        class Provider:
            def collection(self, *args, **kwargs):
                raise SourceHTTPError(401)
        with self.assertRaises(SourceHTTPError):
            Inventory(Provider()).collect('议题', '/issues')

    def test_replaced_repository_cannot_reuse_original_discovery(self):
        class Provider:
            kind = 'gitea'
            def get(self, path):
                return {'id': 99, 'full_name': 'group/repo'}
        with self.assertRaisesRegex(MigrationError, '身份或路径已变化'):
            Inventory(Provider()).inspect({'id': 10, 'path': 'group/repo'})

    def test_string_discussion_identity_keeps_all_pages(self):
        def respond(_method, path, _headers, _body):
            page = int(parse_qs(urlsplit(path).query)['page'][0])
            return 200, {'X-Next-Page': '2' if page == 1 else ''}, [{'id': 'discussion-' + str(page)}]
        with source_server(respond) as base:
            http = Transport(base)
            self.addCleanup(http.close)
            self.assertEqual(len(http.collection('/discussions')), 2)


if __name__ == '__main__':
    unittest.main()
