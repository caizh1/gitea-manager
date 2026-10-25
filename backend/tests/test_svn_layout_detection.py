"""真实隔离 SVN 样本与授权/过期/歧义门禁回归，不访问公司 SVN。"""
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from models import db
from svn_migration.common import MigrationError, layout, encode
from svn_migration.engine import Process, Source, convert, verify_history
from svn_migration.layout_detection import detect, confirmed, queue, step
from svn_migration.models import SvnLayoutDetection
from svn_migration import service
from tests import test_svn_migrations as fixtures


class LayoutApiTests(unittest.TestCase):
    setUp = fixtures.MigrationTests.setUp
    post = fixtures.MigrationTests.post
    source = fixtures.MigrationTests.source

    def result(self):
        return {'revision': 3, 'uuid': '测试标识', 'directories': [
            {'path': 'trunk', 'listed': False}, {'path': 'branches', 'listed': True, 'files': 0}],
            'entries': [], 'candidates': [{'layout': {'trunk': 'trunk', 'branches': 'branches', 'tags': '', 'default_branch': 'main'}}],
            'limited': False, 'warnings': [], 'requests': 2}

    def test_permissions_csrf_and_source_boundaries(self):
        source = self.source()
        route = f'/sources/{source.id}/layout-detections'
        # 写接口的全局 CSRF 在登录校验之前拒绝匿名请求；读接口单独验证登录。
        self.assertEqual(self.app.test_client().post('/api/svn-migrations'+route, json={'urls': [source.url]}).status_code, 403)
        self.assertEqual(self.app.test_client().get('/api/svn-migrations/layout-detections/1').status_code, 401)
        self.csrf = fixtures.login_platform(self.client)
        self.assertEqual(self.client.post('/api/svn-migrations'+route, json={'urls': [source.url]}).status_code, 403)
        self.assertEqual(self.post(route, {'urls': ['svn://different/project']}).status_code, 400)
        self.assertEqual(self.post(route, {'urls': [source.url, source.url+'/']}).status_code, 400)
        response = self.post(route, {'urls': [source.url+'/project/']})
        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertNotIn('测试密码', json.dumps(response.get_json(), ensure_ascii=False))
        from access.models import ManagerAccount
        account = ManagerAccount.query.filter_by(subject='local:fixture').one()
        account.is_platform_admin = False; db.session.commit()
        from flask import g
        g.pop('_login_user', None)
        self.assertEqual(self.client.get('/api/svn-migrations/layout-detections/'+str(response.get_json()[0]['id'])).status_code, 403)

    def test_readonly_worker_and_confirmation_validation(self):
        source = self.source()
        row = queue(source, [source.url+'/project'])[0]
        with patch('svn_migration.layout_detection.detect', return_value=self.result()):
            self.assertTrue(step(lambda: None))
        self.assertEqual(row.status, 'ready')
        data = {'name': 'project', 'url': row.url, 'detection_id': row.id,
                'layout': self.result()['candidates'][0]['layout']}
        repo = service.edit_repository(source.id, data)
        self.assertEqual(json.loads(repo.layout_json)['branches'], 'branches')
        data['layout'] = dict(data['layout'], trunk='invented')
        with self.assertRaisesRegex(MigrationError, '不在检测目录'): confirmed(source, data)
        source.version += 1; db.session.commit()
        with self.assertRaisesRegex(MigrationError, '已失效'): confirmed(source, data)
        response = self.client.get('/api/svn-migrations/layout-detections/'+str(row.id)).get_json()
        self.assertEqual(response['status'], 'stale')

    def test_incomplete_or_failed_probe_cannot_save(self):
        source = self.source(); row = queue(source, [source.url+'/project'])[0]
        data = {'url': row.url, 'detection_id': row.id, 'layout': self.result()['candidates'][0]['layout']}
        with self.assertRaises(MigrationError): confirmed(source, data)
        value = self.result(); value['limited'] = True
        row.status = 'ambiguous'; row.result_json = encode(value); db.session.commit()
        with self.assertRaisesRegex(MigrationError, '扫描达到边界'): confirmed(source, data)
        row.status = 'queued'; db.session.commit()
        with patch('svn_migration.layout_detection.detect', side_effect=MigrationError('读取被拒绝', 'command')):
            step(lambda: None)
        self.assertEqual(row.status, 'failed')
        self.assertIn('读取被拒绝', row.error)

    def test_queue_is_bounded_and_root_cannot_overlap(self):
        source = self.source()
        with self.assertRaises(MigrationError): queue(source, [])
        with self.assertRaises(MigrationError): layout({'trunk': '.', 'branches': 'branches', 'tags': ''})
        self.assertEqual(layout({'trunk': '.', 'branches': '', 'tags': ''})['trunk'], '.')

    def test_interrupted_probe_resumes_and_maintenance_does_not_write_failure(self):
        source = self.source(); row = queue(source, [source.url+'/project'])[0]
        row.status = 'running'; db.session.commit()
        with patch('svn_migration.layout_detection.detect', side_effect=MigrationError('维护中', 'maintenance')):
            step(lambda: None)
        db.session.refresh(row)
        self.assertEqual(row.status, 'running')
        self.assertEqual(row.error, '')
        with patch('svn_migration.layout_detection.detect', return_value=self.result()):
            step(lambda: None)
        self.assertEqual(row.status, 'ready')


class RealSvnLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('svnadmin') or not shutil.which('git'):
            raise unittest.SkipTest('需要 SVN 执行器内的真实工具链')
        cls.temp = tempfile.TemporaryDirectory(); cls.root = Path(cls.temp.name)
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); cls.port = sock.getsockname()[1]; sock.close()
        cls.server = subprocess.Popen(['svnserve', '--daemon', '--foreground', '--listen-host', '127.0.0.1',
                                       '--listen-port', str(cls.port), '--root', str(cls.root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1', cls.port), timeout=.1): break
            except OSError: time.sleep(.05)
        cls.metrics = []

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate(); cls.server.wait(timeout=5)
        print('真实 SVN 目录样本指标：'+json.dumps(cls.metrics, ensure_ascii=False))
        cls.temp.cleanup()

    def command(self, *args):
        return subprocess.check_output(args, stderr=subprocess.PIPE).decode()

    def create(self, name, directories, files=()):
        repo = self.root/name
        self.command('svnadmin', 'create', str(repo))
        (repo/'conf/svnserve.conf').write_text('[general]\nanon-access = none\nauth-access = read\npassword-db = passwd\nrealm = 隔离目录检测\n')
        (repo/'conf/passwd').write_text('[users]\nreader = fixture-password\n')
        with tempfile.TemporaryDirectory() as folder:
            tree = Path(folder)
            for path in directories: (tree/path).mkdir(parents=True, exist_ok=True)
            for path in files:
                file = tree/path; file.parent.mkdir(parents=True, exist_ok=True); file.write_text('隔离目录样本内容\n')
            self.command('svn', 'import', str(tree), 'file://'+str(repo), '-m', '创建隔离目录样本', '--username', 'reader')
        return f'svn://127.0.0.1:{self.port}/{name}'

    def source(self, url, folder, password='fixture-password'):
        return Source({'id': 1, 'url': url, 'username': 'reader', 'password': password}, Process(folder, secrets=(password,)), folder)

    def check(self, name, directories, files, expected, ambiguous=False):
        url = self.create(name, directories, files)
        with tempfile.TemporaryDirectory() as folder:
            source = self.source(url, folder)
            result = detect(source, url)
            self.assertFalse(result['limited'])
            if expected:
                for key, value in expected.items(): self.assertEqual(result['candidates'][0]['layout'][key], value, name)
            self.assertEqual(bool(result['warnings']), ambiguous, name)
            self.metrics.append({'样本': name, '候选数': len(result['candidates']), '需确认': bool(result['warnings']),
                                 '请求数': result['requests'], '秒': result['elapsed_seconds']})
        return result

    def test_fixed_real_directory_benchmark(self):
        self.check('standard', ['trunk','branches/dev','tags/v1'], ['trunk/readme.txt'], {'trunk':'trunk','branches':'branches','tags':'tags'})
        self.check('aliases', ['mainline','devlines/dev','releases/v1'], ['mainline/readme.txt'], {'trunk':'mainline','branches':'devlines','tags':'releases'})
        self.check('uppercase', ['TRUNK','BRANCHES/dev','TAGS/v1'], ['TRUNK/readme.txt'], {'trunk':'TRUNK','branches':'BRANCHES','tags':'TAGS'})
        self.check('trunk-only', ['trunk'], ['trunk/readme.txt'], {'trunk':'trunk','branches':'','tags':''})
        self.check('nested', ['project/trunk','project/branches/dev','project/tags/v1'], ['project/trunk/readme.txt'], {'trunk':'project/trunk','branches':'project/branches','tags':'project/tags'})
        self.check('spaces', ['project one/mainline','project one/branches/dev'], ['project one/mainline/readme.txt'], {'trunk':'project one/mainline','branches':'project one/branches','tags':''})
        multi = self.check('multi-project', ['one/trunk','one/branches','two/trunk','two/branches'], ['one/trunk/readme.txt','two/trunk/readme.txt'], None, True)
        self.assertEqual(len(multi['candidates']),2)
        self.check('ambiguous-main', ['main','trunk','branches'], ['main/readme.txt','trunk/readme.txt'], None, True)
        self.check('flat-root', ['src'], ['readme.txt','src/code.c'], {'trunk':'.','branches':'','tags':''}, True)
        self.check('outside-layout', ['trunk','branches','unclassified'], ['trunk/readme.txt','unclassified/readme.txt'], None, True)
        bad = self.check('container-file', ['trunk','branches'], ['trunk/readme.txt','branches/direct.txt'], None, True)
        self.assertFalse(bad['candidates'])
        self.check('custom-main', ['production','branches/dev','tags/v1'], ['production/readme.txt'], None, True)
        self.check('deep-multi', ['projects/one/trunk','projects/two/deep/trunk'], ['projects/one/trunk/readme.txt','projects/two/deep/trunk/readme.txt'], None, True)
        self.check('nested-outside', ['project/trunk','project/branches','project/extra'], ['project/trunk/readme.txt','project/extra/readme.txt'], None, True)

    def test_scan_limits_and_credentials_failure_are_not_success(self):
        url = self.create('limited', ['a','b','c'], ['a/readme.txt'])
        with tempfile.TemporaryDirectory() as folder:
            source = self.source(url, folder)
            with patch('svn_migration.layout_detection.MAX_LISTS', 1):
                self.assertTrue(detect(source, url)['limited'])
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(MigrationError): detect(self.source(url, folder, 'wrong-password'), url)

    def test_root_and_nested_root_convert_and_verify_full_history(self):
        for name, prefix in [('root-history',''), ('nested-root-history','project/')]:
            url = self.create(name, [prefix+'src'], [prefix+'readme.txt',prefix+'src/code.c'])
            with tempfile.TemporaryDirectory() as checkout:
                self.command('svn', 'checkout', 'file://'+str(self.root/name), checkout)
                (Path(checkout)/(prefix+'src/code.c')).write_text('修改后的隔离代码\n')
                self.command('svn', 'commit', checkout, '-m', '修改主干代码', '--username', 'reader')
                self.command('svn', 'delete', str(Path(checkout)/(prefix+'readme.txt')))
                self.command('svn', 'commit', checkout, '-m', '删除主干文件', '--username', 'reader')
            self.command('svn', 'mkdir', 'file://'+str(self.root/name)+'/unrelated', '-m', '增加布局之外的目录', '--username', 'reader') if prefix else None
            selected_url = url + ('/project' if prefix else '')
            with tempfile.TemporaryDirectory() as folder:
                source = self.source(url, folder)
                selected = {'trunk':'.','branches':'','tags':'','default_branch':'main'}
                snapshot = source.inventory(selected_url, selected)
                authors = service.author_map(1, snapshot['authors'], [])
                expected = convert(source, snapshot, authors, Path(folder), lambda _: None)
                result = verify_history(source, snapshot, authors, Path(folder), expected, lambda _: None)
                self.assertEqual(result['已验证提交数'],3)
                self.assertIn('refs/heads/main',expected)
