"""门禁每次外部写入前重新核验，以及个人审计的确定性回归。"""
import json
import unittest
from unittest.mock import MagicMock, patch

from tests import test_organization_access as fixture
from access.models import AccessAudit, OrganizationGrant
from models import db, GiteaServer, CommitGateAssignment


class GateAccessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.OrganizationAccessTest()
        self.fixture.setUp()
        self.app = self.fixture.app
        self.fetch = patch('services.commit_gate_service.fetch_repos',
                           side_effect=lambda server: list(fixture.DirectoryFixture.repos.values()))
        self.fetch.start()
        self.addCleanup(self.fetch.stop)

    def tearDown(self):
        self.fixture.tearDown()
        self.fixture.doCleanups()

    def revoke(self):
        # 与业务请求使用同一真实数据库；不替换授权实现或伪造回调结果。
        db.session.execute(db.update(OrganizationGrant).where(
            OrganizationGrant.account_id == self.fixture.account_id,
            OrganizationGrant.binding_id == 1).values(enabled=False, version=OrganizationGrant.version + 1))
        db.session.commit()

    def apply(self):
        return self.fixture.request('/api/commit-gates/apply', 'post',
            {'server_id': 1, 'rule_id': 201, 'repo_names': ['alpha/same']})

    def remove(self):
        return self.fixture.request('/api/commit-gates/remove', 'post',
            {'server_id': 1, 'repo_names': ['alpha/same']})

    def seed_assignment(self):
        with self.app.app_context():
            db.session.add(CommitGateAssignment(server_id=1, repo_id=1001, repo_name='alpha/same',
                rule_id=201, install_status='success', install_log='原始安装输出不得进入审计'))
            db.session.commit()

    def audits(self):
        with self.app.app_context():
            rows = AccessAudit.query.order_by(AccessAudit.id).all()
            result = []
            for row in rows:
                detail = json.loads(row.detail_json)
                self.assertEqual(row.actor_id, self.fixture.account_id)
                self.assertEqual(row.actor_name, f'甲部门管理员（账号 {self.fixture.account_id}）')
                self.assertEqual(detail['actor']['account_id'], self.fixture.account_id)
                self.assertEqual(detail['actor']['auth_version'], 1)
                self.assertEqual(detail['actor']['grant_version'], 1)
                self.assertEqual(detail['actor']['binding_id'], 1)
                self.assertFalse(detail['actor']['platform'])
                self.assertEqual(detail['server_id'], 1)
                self.assertEqual(detail['rule_id'], 201)
                self.assertNotIn('install_log', row.detail_json)
                self.assertNotIn('原始安装输出', row.detail_json)
                self.assertNotIn('秘密诊断', row.detail_json)
                result.append((row.action, detail))
            return result

    def test_revocation_after_mkdir_prevents_upload_and_chmod(self):
        def mkdir(server, command):
            self.assertTrue(command.startswith('mkdir -p '))
            self.revoke()
            return 0, '', ''
        with patch('services.commit_gate_service._resolve_repo_dir', return_value='/data/git/repositories/alpha/same.git'), \
                patch('services.commit_gate_service._run_in_gitea', side_effect=mkdir) as execute, \
                patch('services.commit_gate_service._put_content_local') as local, \
                patch('services.commit_gate_service._put_content_remote') as remote:
            response = self.apply()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['results'][0]['install_status'], 'failed')
        execute.assert_called_once(); local.assert_not_called(); remote.assert_not_called()
        self.assertEqual([(action, row['status']) for action, row in self.audits()],
                         [('提交门禁下发', 'pending'), ('提交门禁下发结果', 'failed')])

    def test_revocation_after_local_upload_prevents_chmod(self):
        with self.app.app_context():
            db.session.get(GiteaServer, 1).is_local = True
            db.session.commit()
        docker = MagicMock()
        upload = docker.containers.get.return_value.put_archive
        upload.side_effect = lambda *args: self.revoke()
        with patch('services.commit_gate_service._resolve_repo_dir', return_value='/data/git/repositories/alpha/same.git'), \
                patch('services.commit_gate_service._run_in_gitea', return_value=(0, '', '')) as execute, \
                patch('services.commit_gate_service._get_client', return_value=docker), \
                patch('services.commit_gate_service._put_content_remote') as remote:
            response = self.apply()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['results'][0]['install_status'], 'failed')
        upload.assert_called_once(); remote.assert_not_called(); execute.assert_called_once()
        self.assertTrue(execute.call_args.args[1].startswith('mkdir -p '))

    def test_revocation_during_remote_staging_prevents_docker_copy_but_cleans_temporary_file(self):
        ssh = MagicMock()
        ssh.put_file.side_effect = lambda *args: self.revoke()
        ssh.exec.return_value = (0, '', '')
        with patch('services.commit_gate_service._resolve_repo_dir', return_value='/data/git/repositories/alpha/same.git'), \
                patch('services.commit_gate_service._run_in_gitea', return_value=(0, '', '')) as execute, \
                patch('services.commit_gate_service.SSHService', return_value=ssh):
            response = self.apply()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['results'][0]['install_status'], 'failed')
        ssh.put_file.assert_called_once(); execute.assert_called_once(); ssh.exec.assert_called_once()
        self.assertTrue(ssh.exec.call_args.args[0].startswith('rm -f /tmp/'))
        self.assertNotIn('docker cp', str(ssh.exec.call_args_list))

    def test_authorized_remote_install_still_copies_and_enables_hook(self):
        ssh = MagicMock()
        ssh.exec.return_value = (0, '', '')
        with patch('services.commit_gate_service._resolve_repo_dir', return_value='/data/git/repositories/alpha/same.git'), \
                patch('services.commit_gate_service._run_in_gitea', return_value=(0, '', '')) as execute, \
                patch('services.commit_gate_service.SSHService', return_value=ssh):
            response = self.apply()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['results'][0]['install_status'], 'success')
        ssh.put_file.assert_called_once()
        self.assertEqual(len(ssh.exec.call_args_list), 2)
        self.assertTrue(ssh.exec.call_args_list[0].args[0].startswith('docker cp '))
        self.assertTrue(ssh.exec.call_args_list[1].args[0].startswith('rm -f /tmp/'))
        self.assertEqual(len(execute.call_args_list), 2)
        self.assertTrue(execute.call_args_list[0].args[1].startswith('mkdir -p '))
        self.assertTrue(execute.call_args_list[1].args[1].startswith('chmod +x '))

    def test_removal_rechecks_after_resolving_repository_path(self):
        self.seed_assignment()
        def resolve(*args):
            self.revoke()
            return '/data/git/repositories/alpha/same.git'
        with patch('services.commit_gate_service._resolve_repo_dir', side_effect=resolve), \
                patch('services.commit_gate_service._run_in_gitea') as execute:
            response = self.remove()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['results'][0]['status'], 'failed')
        execute.assert_not_called()
        with self.app.app_context():
            self.assertEqual(CommitGateAssignment.query.one().repo_id, 1001)
        self.assertEqual([(action, row['status']) for action, row in self.audits()],
                         [('提交门禁移除', 'pending'), ('提交门禁移除结果', 'failed')])

    def test_apply_remove_persist_real_identity_versions_and_result_status(self):
        with patch('services.commit_gate_service._install_hook'), patch('services.commit_gate_service._remove_hook'):
            installed, removed = self.apply(), self.remove()
        self.assertEqual(installed.status_code, 200, installed.json)
        self.assertEqual(removed.status_code, 200, removed.json)
        self.assertEqual(removed.json['results'][0]['status'], 'success')
        audits = self.audits()
        self.assertEqual([(action, row['status']) for action, row in audits], [
            ('提交门禁下发', 'pending'), ('提交门禁下发结果', 'success'),
            ('提交门禁移除', 'pending'), ('提交门禁移除结果', 'success')])
        self.assertEqual(audits[0][1]['rule_snapshot']['pattern'], '.*')
        self.assertTrue(all(row['repo_id'] == 1001 for action, row in audits))
        with self.app.app_context():
            self.assertEqual(CommitGateAssignment.query.count(), 0)

    def test_failed_operation_audit_does_not_copy_raw_install_output(self):
        diagnostic = '秘密诊断：SSH 路径、令牌及安装原始输出'
        with patch('services.commit_gate_service._install_hook', side_effect=RuntimeError(diagnostic)):
            response = self.apply()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['results'][0]['install_status'], 'failed')
        self.assertNotIn(diagnostic, json.dumps(response.json, ensure_ascii=False))
        self.assertEqual(self.audits()[-1][1]['status'], 'failed')
        with self.app.app_context():
            self.assertEqual(CommitGateAssignment.query.one().install_log, diagnostic,
                             '原始诊断保留平台排障，审计只记录业务状态')
        with patch('services.commit_gate_service._install_hook', side_effect=RuntimeError(diagnostic)):
            admin = self.fixture.request('/api/commit-gates/apply', 'post',
                {'server_id': 1, 'rule_id': 201, 'repo_names': ['alpha/same']},
                client=self.fixture.admin, binding=None)
        self.assertEqual(admin.status_code, 200, admin.json)
        self.assertEqual(admin.json['results'][0]['install_log'], diagnostic)

    def test_remove_response_and_audit_hide_raw_diagnostics_from_organization(self):
        self.seed_assignment()
        diagnostic = '秘密诊断：移除脚本的原始标准错误'
        with patch('services.commit_gate_service._remove_hook', side_effect=RuntimeError(diagnostic)):
            response = self.remove()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['results'][0]['status'], 'failed')
        self.assertNotIn(diagnostic, json.dumps(response.json, ensure_ascii=False))
        self.assertEqual(self.audits()[-1][1]['status'], 'failed')
        with patch('services.commit_gate_service._remove_hook', side_effect=RuntimeError(diagnostic)):
            admin = self.fixture.request('/api/commit-gates/remove', 'post',
                {'server_id': 1, 'repo_names': ['alpha/same']}, client=self.fixture.admin, binding=None)
        self.assertEqual(admin.status_code, 200, admin.json)
        self.assertEqual(admin.json['results'][0]['log'], diagnostic)


if __name__ == '__main__':
    unittest.main()
