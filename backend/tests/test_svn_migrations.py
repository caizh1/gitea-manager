"""迁移边界回归：真实数据库、权限入口、进程取消与恢复占用。"""
import json
import os
from pathlib import Path
import sys
import subprocess
import time
import tempfile
import unittest
from unittest.mock import patch, Mock
import xml.etree.ElementTree as ET

from flask import Flask
from models import db, GiteaServer
from svn_migration.common import MigrationError, Cancelled, source_url, layout, decrypt, digest
from svn_migration.models import SvnSource, SvnRepository, SvnMigrationPlan, SvnMigrationRun, SvnReservation
from svn_migration import service
from svn_migration.engine import Process, Source, reap_processes, revision_coverage
from svn_migration.gitea import Gitea
from routes.svn_migration_routes import svn_migration_bp
from access.control import install
from tests.access_helpers import init_test_auth, login_platform


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Flask(__name__)
        self.app.secret_key = '隔离测试会话密钥仅用于本机测试' * 3
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///' + self.temp.name + '/test.db', TESTING=True)
        init_test_auth(self.app)
        self.app.register_blueprint(svn_migration_bp, url_prefix='/api/svn-migrations')
        install(self.app)
        self.context = self.app.app_context(); self.context.push()
        self.addCleanup(self.context.pop)
        self.addCleanup(db.session.remove)
        self.client = self.app.test_client()
        self.csrf = login_platform(self.client)
        self.target = GiteaServer(name='隔离目标',host='gitea',ssh_user='',gitea_port=3000,
            gitea_url='http://gitea:3000',api_token='仅测试使用的令牌')
        db.session.add(self.target); db.session.commit()

    def post(self, path, value):
        return self.client.post('/api/svn-migrations' + path, json=value, headers={'X-CSRF-Token': self.csrf})

    def source(self, name='team-a'):
        return service.source_edit({'name': '来源 ' + name, 'slug': name, 'url': 'svn://' + name,
                                    'username': 'reader', 'password': '测试密码'})

    def plan(self):
        source = self.source()
        repo = service.edit_repository(source.id, {'name': 'project', 'url': source.url + '/project'})
        plan = service.plan_edit({'name': '测试计划', 'server_id': self.target.id, 'repositories': [{'id': repo.id}]})
        preview = {'inputs': service.input_state(plan), 'repositories': [{'repository_id': repo.id,'owner':'svn-team-a','name':'project'}]}
        plan.preview_json, plan.preview_hash, plan.status = service.encode(preview), digest(preview), 'ready'
        db.session.commit()
        return source, repo, plan

    def run_data(self, plan, key='fixture-1'):
        return {'plan_id':plan.id,'preview_hash':plan.preview_hash,'request_key':key,'freeze_confirmed':True,'full_read_confirmed':True}

    def test_credentials_are_encrypted_and_not_exposed(self):
        source = self.source()
        self.assertNotIn('测试密码', source.password_ciphertext)
        self.assertEqual(decrypt(source.password_ciphertext), '测试密码')
        result = self.client.get('/api/svn-migrations/sources').get_json()
        self.assertNotIn('测试密码', json.dumps(result,ensure_ascii=False))
        self.assertTrue(result[0]['password_set'])

    def test_missing_key_cannot_replace_existing_ciphertext(self):
        source = self.source()
        (Path(self.temp.name) / 'svn-migration.key').unlink()
        with self.assertRaisesRegex(MigrationError, '禁止生成替代'):
            self.source('team-b')
        db.session.rollback()
        self.assertEqual(SvnSource.query.count(), 1)

    def test_url_and_layout_boundaries(self):
        for url in ('file:///etc/passwd','svn+ssh://host/repo','https://name:password@host/repo','http://host/../secret','http://host/a%0Ab'):
            with self.subTest(url=url), self.assertRaises(MigrationError): source_url(url)
        self.assertEqual(layout({'branches':'','tags':''})['trunk'],'trunk')
        with self.assertRaises(MigrationError): layout({'trunk':'src','branches':'src/branches'})

    def test_source_repositories_cannot_cross_hosts(self):
        source = self.source()
        with self.assertRaises(MigrationError):
            service.edit_repository(source.id, {'name':'project','url':'svn://different/project'})

    def test_bulk_import_failure_rolls_back_all_rows(self):
        source=self.source()
        result=self.post(f'/sources/{source.id}/repositories',{'repositories':[
            {'name':'valid','url':source.url+'/valid'}, {'name':'invalid','url':'svn://wrong/repo'}]})
        self.assertEqual(result.status_code,400)
        self.assertEqual(SvnRepository.query.count(),0)

    def test_independent_sources_do_not_merge_authors(self):
        a=service.author_map(1,['alice'],[])
        b=service.author_map(2,['alice'],[])
        self.assertNotEqual(a['alice']['email'],b['alice']['email'])
        self.assertTrue(a['alice']['email'].endswith('.invalid'))

    def test_author_csv_rejects_duplicates_and_injection(self):
        csv='source_id,svn_user,name,email\n1,alice,甲,a@example.test\n1,alice,乙,b@example.test'
        with self.assertRaises(MigrationError):service.authors_config(csv)
        with self.assertRaises(MigrationError):service.authors_config([{'source_id':1,'svn_user':'a=b','name':'甲','email':'a@example.test'}])

    def test_csrf_and_authentication(self):
        anonymous=self.app.test_client()
        self.assertEqual(anonymous.get('/api/svn-migrations/sources').status_code,401)
        self.assertEqual(self.client.post('/api/svn-migrations/sources',json={}).status_code,403)

    def test_non_platform_account_is_rejected(self):
        from access.models import ManagerAccount
        row=ManagerAccount.query.filter_by(subject='local:fixture').one()
        row.is_platform_admin=False;db.session.commit()
        self.assertEqual(self.client.get('/api/svn-migrations/sources').status_code,403)

    def test_stale_input_and_missing_confirmation_rejected(self):
        source,repo,plan=self.plan()
        data=self.run_data(plan);data['full_read_confirmed']=False
        self.assertEqual(self.post('/runs',data).status_code,400)
        source.version+=1;db.session.commit()
        self.assertEqual(self.post('/runs',self.run_data(plan)).status_code,409)
        self.assertEqual(SvnMigrationRun.query.count(),0)

    def test_idempotence_and_active_reservation(self):
        source,repo,plan=self.plan()
        a=self.post('/runs',self.run_data(plan));self.assertEqual(a.status_code,202,a.get_json())
        b=self.post('/runs',self.run_data(plan));self.assertEqual(a.get_json()['id'],b.get_json()['id'])
        self.assertEqual(self.post('/runs',self.run_data(plan,'second')).status_code,409)
        self.assertEqual(SvnMigrationRun.query.count(),1)
        self.assertEqual(SvnReservation.query.count(),2)
        bad=self.run_data(plan);bad['preview_hash']='不同预览'
        self.assertEqual(self.post('/runs',bad).status_code,409)

    def test_cancel_and_resume_restore_reservations(self):
        _,_,plan=self.plan()
        row=self.post('/runs',self.run_data(plan)).get_json()
        self.assertEqual(self.post(f'/runs/{row["id"]}/cancel',{}).status_code,202)
        self.assertEqual(SvnReservation.query.count(),0)
        self.assertEqual(self.post(f'/runs/{row["id"]}/resume',{}).status_code,202)
        self.assertEqual(SvnReservation.query.count(),2)

    def test_live_source_edit_is_rejected(self):
        source,_,plan=self.plan()
        self.post('/runs',self.run_data(plan))
        data=service.public_source(source)
        with self.assertRaisesRegex(MigrationError,'未完成迁移'):service.source_edit(data,source.id)

    def test_process_cancellation_stops_child(self):
        calls=[]
        def tick():
            calls.append(1)
            if len(calls)>2:raise Cancelled()
        process=Process(Path(self.temp.name)/'process',tick)
        started=service.time.monotonic()
        with self.assertRaises(Cancelled):process.run([sys.executable,'-c','import time;time.sleep(30)'])
        self.assertLess(service.time.monotonic()-started,10)

    @unittest.skipUnless(Path('/proc').exists(), '孤儿进程回收仅在 Linux 执行')
    def test_worker_crash_orphan_is_reaped_by_marker(self):
        folder=Path(self.temp.name)/'orphan';folder.mkdir()
        code='from svn_migration.engine import Process; import sys; Process(sys.argv[1]).run([sys.executable,"-c","import time;time.sleep(120)"])'
        supervisor=subprocess.Popen([sys.executable,'-c',code,str(folder)])
        try:
            for _ in range(100):
                markers=list(folder.glob('.process-*.json'))
                if markers:break
                time.sleep(.05)
            self.assertTrue(markers)
            pid=json.loads(markers[0].read_text())['进程']
            supervisor.kill();supervisor.wait()
            reap_processes(folder)
            for _ in range(50):
                status=Path(f'/proc/{pid}/stat')
                if not status.exists() or status.read_text().split()[2]=='Z':break
                time.sleep(.05)
            self.assertTrue(not status.exists() or status.read_text().split()[2]=='Z')
            self.assertEqual(list(folder.glob('.process-*.json')),[])
        finally:
            if supervisor.poll() is None:supervisor.kill();supervisor.wait()
            reap_processes(folder)

    def test_disk_guard_runs_before_subprocess(self):
        process=Process(Path(self.temp.name)/'disk')
        with patch('svn_migration.engine.shutil.disk_usage') as usage, patch('svn_migration.engine.subprocess.Popen') as spawn:
            usage.return_value.free=0
            with self.assertRaisesRegex(MigrationError,'空间不足'):process.run([sys.executable,'-c','pass'])
            spawn.assert_not_called()

    def test_group_members_pagination_checks_every_page(self):
        obj=object.__new__(Gitea)
        with patch.object(obj,'api',side_effect=[[{'user_id':1}],[{'user_id':99}],[]]):
            self.assertEqual([x['user_id'] for x in obj.members('/members')],[1,99])

    def test_preflight_errors_do_not_write_gitea(self):
        _,_,plan=self.plan()
        from svn_migration.worker import preflight
        with patch('svn_migration.worker.Gitea') as fake, patch('svn_migration.worker.Source') as svn:
            fake.return_value.check.return_value={'id':1,'login':'test'}
            svn.return_value.inventory.side_effect=MigrationError('检测到历史 externals','externals')
            preflight(plan.id)
            self.assertEqual(plan.status,'blocked')
            self.assertEqual(json.loads(plan.preview_json)['issues'][0]['code'],'externals')
            self.assertFalse(fake.return_value.api.called)

    def test_preflight_history_shared_by_plan_and_repository_and_keeps_old_input(self):
        source, repo, plan = self.plan()
        from svn_migration.models import SvnPreflight
        from svn_migration.worker import preflight
        with patch('svn_migration.worker.Gitea') as fake, patch('svn_migration.worker.Source') as svn:
            fake.return_value.check.return_value = {'id': 1}
            svn.return_value.inventory.side_effect = MigrationError('检测到历史 externals', 'externals')
            service.queue_preflight(plan)
            preflight(plan.id)
        summary = self.client.get(f'/api/svn-migrations/plans/{plan.id}').get_json()['preflight']
        result = self.client.get(f'/api/svn-migrations/sources/{source.id}/repositories').get_json()[0]
        self.assertEqual(result['preflight']['id'], summary['id'])
        self.assertEqual(result['preflight']['repository_status'], 'blocked')
        self.assertNotIn('migration', result)
        self.assertTrue(summary['current'])
        old = self.client.get(f'/api/svn-migrations/preflights/{summary["id"]}').get_json()
        service.edit_repository(source.id, {**service.public_repo(repo), 'name': 'renamed'}, repo.id)
        stale = self.client.get(f'/api/svn-migrations/preflights/{summary["id"]}').get_json()
        self.assertFalse(stale['current'])
        self.assertEqual(stale['preview'], old['preview'])
        service.queue_preflight(plan)
        self.assertEqual(SvnPreflight.query.count(), 2)
        newest = SvnPreflight.query.order_by(SvnPreflight.id.desc()).first()
        old_detail = self.client.get(f'/api/svn-migrations/preflights/{summary["id"]}').get_json()
        new_detail = self.client.get(f'/api/svn-migrations/preflights/{newest.id}').get_json()
        self.assertEqual(old_detail['next_id'], newest.id)
        self.assertEqual(new_detail['previous_id'], summary['id'])
        self.assertEqual(db.session.get(SvnPreflight, summary['id']).status, 'blocked')

    def test_global_preflight_failure_never_marks_repository_ready(self):
        source, _, plan = self.plan()
        from svn_migration.worker import preflight
        with patch('svn_migration.worker.Gitea') as fake:
            fake.return_value.check.side_effect = MigrationError('目标身份检查失败', 'authorization')
            service.queue_preflight(plan)
            preflight(plan.id)
        result = self.client.get(f'/api/svn-migrations/sources/{source.id}/repositories').get_json()[0]
        self.assertEqual(result['preflight']['repository_status'], 'blocked')
        self.assertEqual(result['preflight']['issues'][0]['message'], '目标身份检查失败')
        self.assertTrue(result['preflight']['current'])

    def test_preflight_backfill_is_idempotent_and_keeps_latest_legacy_preview(self):
        _, _, plan = self.plan()
        from svn_migration.preflights import backfill
        from svn_migration.models import SvnPreflight
        backfill(); backfill()
        self.assertEqual(SvnPreflight.query.count(), 1)
        checked = SvnPreflight.query.one()
        self.assertEqual(json.loads(checked.preview_json), json.loads(plan.preview_json))
        self.assertEqual(checked.plan_version, plan.version)

    def test_migration_success_is_independent_of_latest_preflight_or_failed_run(self):
        source, repo, plan = self.plan()
        from svn_migration.models import SvnMigrationItem
        first = self.post('/runs', self.run_data(plan, 'first')).get_json()
        run = db.session.get(SvnMigrationRun, first['id'])
        item = SvnMigrationItem.query.filter_by(run_id=run.id).one()
        run.status = item.status = 'success'
        service.release_reservations(run); db.session.commit()
        second = self.post('/runs', self.run_data(plan, 'second')).get_json()
        db.session.get(SvnMigrationRun, second['id']).status = 'failed'
        SvnMigrationItem.query.filter_by(run_id=second['id']).one().status = 'failed'
        db.session.commit()
        result = self.client.get(f'/api/svn-migrations/sources/{source.id}/repositories').get_json()[0]
        self.assertEqual(result['last_success']['run_id'], first['id'])
        self.assertEqual(result['migration']['run_id'], second['id'])
        self.assertEqual(result['migration']['status'], 'failed')

    def test_run_summary_uses_immutable_plan_and_preflight_snapshot(self):
        _, _, plan = self.plan()
        from svn_migration.preflights import backfill
        backfill()
        result = self.post('/runs', self.run_data(plan)).get_json()
        self.assertEqual(result['plan_name'], '测试计划')
        self.assertEqual(result['plan_version'], 1)
        self.assertIsNotNone(result['preflight_id'])
        plan.name, plan.version = '后续编辑', 2; db.session.commit()
        result = self.client.get(f'/api/svn-migrations/runs/{result["id"]}').get_json()
        self.assertEqual(result['plan_name'], '测试计划')
        self.assertEqual(result['plan_version'], 1)

    def test_historical_copied_tree_checks_inherited_externals(self):
        source=object.__new__(Source)
        source.info=Mock(return_value={'uuid':'test','root':'svn://example/repo','url':'svn://example/repo','revision':3,'path':'/'})
        source.xml=Mock(return_value=ET.fromstring('<lists><list/></lists>'))
        source.process=Mock()
        source.properties=Mock(side_effect=[{}, {'svn://example/repo/trunk/copied':{'svn:externals':'^/external'}}])
        log=Path(self.temp.name)/'copied-history.xml'
        log.write_text('<log><logentry revision="2"><author>alice</author><paths><path kind="dir" action="A" prop-mods="false" copyfrom-path="/elsewhere" copyfrom-rev="1">/trunk/copied</path></paths></logentry></log>')
        source.file=Mock(return_value=log)
        with self.assertRaisesRegex(MigrationError,'externals'):
            source.inventory('svn://example/repo',{'trunk':'trunk','branches':'','tags':'','default_branch':'main'})
        self.assertTrue(source.properties.call_args.kwargs['recursive'])

    def test_inventory_write_is_committed_before_remote_group_check(self):
        _,_,plan=self.plan()
        from svn_migration.worker import preflight
        inventory={'uuid':'fixed','revision':1,'authors':['alice'],'branches':[],'tags':[]}
        def group(*_):
            self.assertFalse(db.session.dirty, '远端检查前不得保留待刷新的盘点写入')
            return {'owner':'svn-test','group_id':None,'group_name':'隔离来源'}
        with patch('svn_migration.worker.Gitea') as api, patch('svn_migration.worker.Source') as svn, patch('svn_migration.worker.select_group',side_effect=group):
            api.return_value.check.return_value={'id':1,'login':'test'}
            api.return_value.repository.return_value=None
            svn.return_value.inventory.return_value=inventory
            preflight(plan.id)
            self.assertEqual(plan.status,'ready')

    def test_project_copy_collects_parent_authors_before_execution(self):
        source=object.__new__(Source)
        source.info=Mock(return_value={'uuid':'test','root':'svn://example/repo','url':'svn://example/repo/projects/new','revision':3,'path':'/projects/new'})
        source.xml=Mock(return_value=ET.fromstring('<lists><list/></lists>'))
        source.process=Mock();source.properties=Mock(return_value={})
        first=Path(self.temp.name)/'project-log.xml';parent=Path(self.temp.name)/'parent-log.xml'
        first.write_text('<log><logentry revision="3"><author>alice</author><paths><path kind="dir" action="A" prop-mods="false" copyfrom-path="/old/src" copyfrom-rev="2">/projects/new/src</path></paths></logentry></log>')
        parent.write_text('<log><logentry revision="2"><author>bob</author><paths><path kind="file" action="M">/old/src/readme</path></paths></logentry></log>')
        source.file=Mock(side_effect=[first,parent])
        result=source.inventory('svn://example/repo/projects/new',{'trunk':'src','branches':'','tags':'','default_branch':'main'})
        self.assertEqual(result['authors'],['alice','bob'])
        self.assertEqual(set(result['history']),{'2','3'})
        self.assertEqual(source.file.call_count,2)


class RevisionCoverageTests(unittest.TestCase):
    def snapshot(self, paths):
        return {'path':'/', 'root':'svn://example/repository',
                'layout':{'trunk':'trunk','branches':'branches','tags':'tags'},
                'history':{'3':{'paths':paths}}}

    def mapping(self, path, rev=3):
        return {'路径':'svn://example/repository/'+path,'修订':rev}

    def test_missing_file_revision_is_not_hidden_by_other_branch(self):
        snapshot=self.snapshot([{'path':'/trunk/readme','kind':'file','action':'M'},
                                {'path':'/branches/feature/readme','kind':'file','action':'M'}])
        with self.assertRaisesRegex(MigrationError,'没有对应'):
            revision_coverage(Mock(),snapshot,[self.mapping('trunk')])
        self.assertEqual(len(revision_coverage(Mock(),snapshot,[self.mapping('trunk'),self.mapping('branches/feature')])[0]['已映射路径']),2)

    def test_empty_directory_revision_is_explained(self):
        snapshot=self.snapshot([{'path':'/trunk/empty','kind':'dir','action':'A'}])
        result=revision_coverage(Mock(),snapshot,[])
        self.assertTrue(result[0]['未生成独立提交的原因'])
        self.assertFalse(result[0]['已映射路径'])

    def test_deleted_branch_requires_reachable_history(self):
        snapshot=self.snapshot([{'path':'/branches/old','kind':'dir','action':'D'}])
        source=Mock();source.xml.return_value=ET.fromstring('<lists><list><entry kind="file"/></list></lists>')
        with self.assertRaisesRegex(MigrationError,'缺少可达'):
            revision_coverage(source,snapshot,[])
        self.assertTrue(revision_coverage(source,snapshot,[self.mapping('branches/old',2)])[0]['未生成独立提交的原因'])

    def test_copied_directory_and_mode_change_cannot_be_skipped(self):
        source=Mock();source.xml.return_value=ET.fromstring('<lists><list><entry kind="file"/></list></lists>')
        snapshot=self.snapshot([{'path':'/trunk/imported','kind':'dir','action':'A','copyfrom-path':'/elsewhere'}])
        with self.assertRaisesRegex(MigrationError,'没有对应'):
            revision_coverage(source,snapshot,[])
        snapshot=self.snapshot([{'path':'/trunk/run.sh','kind':'file','action':'M','text-mods':'false'}])
        url='svn://example/repository/trunk/run.sh'
        source.properties.side_effect=[{url:{}},{url:{'svn:executable':'*'}}]
        with self.assertRaisesRegex(MigrationError,'没有对应'):
            revision_coverage(source,snapshot,[])
        source.properties.side_effect=[{url:{'svn:eol-style':'LF'}},{url:{'svn:eol-style':'native'}}]
        self.assertTrue(revision_coverage(source,snapshot,[])[0]['未生成独立提交的原因'])


if __name__=='__main__':unittest.main()
