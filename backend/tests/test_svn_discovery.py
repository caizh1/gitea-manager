"""实例发现、完整项目纳管与真实 SVN 转换回归。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from models import db
from svn_migration import discovery, service
from svn_migration.common import MigrationError, encode
from svn_migration.models import SvnDiscovery, SvnRepository
from svn_migration.engine import convert, verify_history
from tests import test_svn_migrations as fixtures
from tests import test_svn_layout_detection as real


class DiscoveryTests(unittest.TestCase):
    setUp = fixtures.MigrationTests.setUp
    post = fixtures.MigrationTests.post
    source = fixtures.MigrationTests.source

    def ready(self, source):
        row=discovery.queue(source)
        row.status='ready'; row.result_json=encode({'projects':[
            {'key':'a','name':'emmc','label':'emmc','url':source.url+'/emmc','uuid':'a','entries':[{'name':'test_spec','kind':'dir'}]},
            {'key':'b','name':'other','label':'other','url':source.url+'/other','uuid':'b','entries':[]}]})
        db.session.commit(); return row

    def test_index_links_are_same_source_direct_children(self):
        html='<a href="emmc/">x</a><a href="../outside/">x</a><a href="https://evil.test/x">x</a><a href="emmc/trunk">x</a><a href="b%20one/">x</a>'
        self.assertEqual(discovery.index_links(html,'https://host.test/svn'),['https://host.test/svn/b%20one','https://host.test/svn/emmc'])
        self.assertEqual(discovery.index_links('<svn><index><dir name="emmc" href="emmc/"/></index></svn>','https://host.test/svn'),['https://host.test/svn/emmc'])

    def test_queue_identity_and_source_boundary(self):
        source=self.source(); first=discovery.queue(source)
        self.assertEqual(discovery.queue(source).id,first.id)
        with self.assertRaises(MigrationError):discovery.queue(source,'http://other/private')

    def test_enroll_atomic_idempotent_and_complete(self):
        source=self.source(); row=self.ready(source)
        with self.assertRaises(MigrationError):discovery.enroll(source,{'discovery_id':row.id,'keys':['a','forged']})
        self.assertEqual(SvnRepository.query.count(),0)
        result=discovery.enroll(source,{'discovery_id':row.id,'keys':['a','b']})
        self.assertEqual(len(result),2)
        self.assertTrue(all(json.loads(r.layout_json)['project'] for r in result))
        again=discovery.enroll(source,{'discovery_id':row.id,'keys':['a']})
        self.assertEqual(again[0].id,result[0].id)
        self.assertEqual(SvnRepository.query.count(),2)
        self.assertEqual(discovery.public(row)['result']['projects'][0]['managed_id'],result[0].id)

    def test_stale_cross_source_and_incomplete_block_enroll(self):
        source=self.source(); row=self.ready(source); other=self.source('other')
        for current in [other]:
            with self.assertRaises(MigrationError):discovery.enroll(current,{'discovery_id':row.id,'keys':['a']})
        row.status='running';db.session.commit()
        with self.assertRaises(MigrationError):discovery.enroll(source,{'discovery_id':row.id,'keys':['a']})
        row.status='ready';source.version+=1;db.session.commit()
        self.assertEqual(discovery.public(row)['status'],'stale')
        with self.assertRaises(MigrationError):discovery.enroll(source,{'discovery_id':row.id,'keys':['a']})

    def test_routes_enforce_auth_csrf_and_whitelist(self):
        source=self.source()
        self.assertEqual(self.client.post(f'/api/svn-migrations/sources/{source.id}/discoveries',json={}).status_code,403)
        response=self.post(f'/sources/{source.id}/discoveries',{})
        self.assertEqual(response.status_code,202,response.get_json())
        self.assertEqual(self.client.get('/api/svn-migrations/discoveries/'+str(response.get_json()['id'])).status_code,200)
        self.assertEqual(self.post(f'/sources/{source.id}/enroll',{'discovery_id':response.get_json()['id'],'keys':['a']}).status_code,400)
        self.assertEqual(self.post(f'/sources/{source.id}/test',{}).status_code,202)
        self.assertNotIn('测试密码',json.dumps(response.get_json(),ensure_ascii=False))

    def test_worker_restart_and_maintenance(self):
        source=self.source();row=discovery.queue(source);row.status='running';db.session.commit()
        with patch('svn_migration.discovery.discover',side_effect=MigrationError('维护中','maintenance')):
            discovery.step(lambda:None)
        self.assertEqual(row.status,'running')
        with patch('svn_migration.discovery.discover',return_value={'projects':[],'connected':True,'message':'未开放列表'}):
            discovery.step(lambda:None)
        self.assertEqual(row.status,'unavailable')
        self.assertEqual(json.loads(source.test_json)['status'],'success')


class RealProjectTests(unittest.TestCase):
    setUpClass=classmethod(real.RealSvnLayoutTests.setUpClass.__func__)
    tearDownClass=classmethod(real.RealSvnLayoutTests.tearDownClass.__func__)
    command=real.RealSvnLayoutTests.command
    create=real.RealSvnLayoutTests.create
    source=real.RealSvnLayoutTests.source

    def test_whole_project_preserves_extra_history_and_native_branches(self):
        url=self.create('full-project',['trunk','branches/dev','tags/v1','test_spec'],['trunk/code.c','test_spec/spec.txt','branches/dev/code.c','tags/v1/code.c'])
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=self.source(url,root)
            for number in (2,3):
                (root/'spec.txt').write_text('第 ' + str(number) + ' 次测试规范修改\n')
                self.command('svnmucc','--username','reader','--password','fixture-password','--non-interactive','-m','修改外围测试规范','put',str(root/'spec.txt'),'file://'+str(self.root/'full-project/test_spec/spec.txt'))
            snapshot=source.inventory(url,{'trunk':'.','branches':'','tags':'','default_branch':'main','project':True})
            self.assertEqual(snapshot['layout']['trunk'],'trunk')
            self.assertIn('project_ref',snapshot)
            authors=service.author_map(1,snapshot['authors'],[])
            refs=convert(source,snapshot,authors,root,lambda _:None)
            result=verify_history(source,snapshot,authors,root,refs,lambda _:None)
            self.assertIn('refs/heads/main',refs);self.assertIn('refs/heads/dev',refs);self.assertIn('refs/tags/v1',refs)
            files=source.process.run(['git','ls-tree','-r','--name-only',snapshot['project_ref']],cwd=root/'converted').decode()
            self.assertIn('test_spec/spec.txt',files)
            self.assertEqual(len(result['完整项目修订覆盖']),3)
            self.assertGreaterEqual(result['已验证提交数'],4)

    def test_svn_directory_discovery_does_not_split_test_spec(self):
        url=self.create('single-project',['trunk','branches','test_spec'],['trunk/code.c','test_spec/spec.txt'])
        with tempfile.TemporaryDirectory() as folder:
            source=self.source(url,folder);seen=[]
            result=discovery.discover(source.record,url,source.process,folder,lambda *v:seen.append(v))
            self.assertEqual(len(result['projects']),1)
            self.assertEqual(result['projects'][0]['url'],url)
            self.assertIn('test_spec',[r['name'] for r in result['projects'][0]['entries']])

    def test_shared_repository_lists_two_projects_without_splitting_their_directories(self):
        url=self.create('shared', ['a/trunk','a/test_spec','b/trunk'],['a/trunk/a.txt','a/test_spec/spec.txt','b/trunk/b.txt'])
        with tempfile.TemporaryDirectory() as folder:
            source=self.source(url,folder)
            result=discovery.discover(source.record,url,source.process,folder,lambda *v:None)
            self.assertEqual([p['label'] for p in result['projects']],['a','b'])
            self.assertIn('test_spec',[e['name'] for e in result['projects'][0]['entries']])

    def test_fingerprint_ignores_property_response_order(self):
        url=self.create('property-order',['trunk','test_spec'],['trunk/code.c','test_spec/spec.txt'])
        with tempfile.TemporaryDirectory() as folder:
            source=self.source(url,folder)
            original=source.properties
            def properties(*args,**kwargs):
                value=original(*args,**kwargs)
                value[url+'/trunk/code.c']={'svn:mime-type':'text/plain'}
                value[url+'/test_spec/spec.txt']={'svn:mime-type':'text/plain'}
                return value
            source.properties=properties
            config={'trunk':'.','branches':'','tags':'','default_branch':'main','project':True}
            first=source.inventory(url,config)
            source.properties=lambda *a,**kw:dict(reversed(list(properties(*a,**kw).items())))
            second=source.inventory(url,config)
            self.assertEqual(first['fingerprint'],second['fingerprint'])
            self.assertEqual(first['properties'],second['properties'])
