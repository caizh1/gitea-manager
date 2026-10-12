"""用户现场失败场景的固定回归；仅使用隔离数据库、合成源码与受控服务替身。"""
import concurrent.futures
import contextlib
import json
import sqlite3
import threading
import time
import unittest
from unittest.mock import Mock, patch

import test_ai_review as baseline_store
from test_ai_review import example
import test_ai_review_setup as baseline_setup
from ai_review.common import ReviewError, Stale
from ai_review.store import Store
from ai_review.setup_service import submit, resume, public_job, update, execute, verify_model
from ai_review.host_policy import gitea_address, read
from ai_review.engine import review
from ai_review.snapshot import work_items
from ai_review.diagnostics import describe
from models import db, GiteaServer
from routes.ai_review_routes import repository_values


class SetupRegressionTests(unittest.TestCase):
    setUp = baseline_setup.SetupTests.setUp
    login = baseline_setup.SetupTests.login

    def test_model_changed_during_probe_cannot_enable_or_start_container_operations(self):
        created=submit(self.store,self.payload,'http://manager.internal')
        with self.store.connect() as c:job=c.execute('SELECT * FROM setup_jobs WHERE id=?',(created['id'],)).fetchone()
        def changed(*args):self.store.save_config({'revision':1,'enabled':False})
        with patch('ai_review.setup_service.verify_model',side_effect=changed),patch('ai_review.setup_service.ContainerAccess') as transport:
            with self.assertRaisesRegex(ReviewError,'检查期间发生变化'):execute(self.store,job)
        self.assertFalse(transport.called);self.assertFalse(self.store.config()['enabled'])

    def test_cross_instance_rejected_before_approving_hosts_or_transport(self):
        other = GiteaServer(name='Gitea 备',api_token='隔离测试凭据',ssh_user='root',gitea_port=3000,host='backup.internal',gitea_url='backup.internal:3000')
        db.session.add(other);db.session.commit()
        payload = dict(self.payload, server_id=other.id)
        before = read()
        with patch('ai_review.setup_service.ContainerAccess') as transport, self.assertRaises(ReviewError):
            submit(self.store,payload,'http://manager.internal')
        self.assertFalse(transport.called);self.assertEqual(before,read())
        self.assertIsNone(public_job(self.store))

    def test_missing_explicit_instance_rejects_old_cached_page(self):
        payload = dict(self.payload);payload.pop('server_id')
        with self.assertRaises(ReviewError):submit(self.store,payload,'http://manager.internal')

    def test_resume_uses_current_saved_model_and_clears_previous_results(self):
        job = submit(self.store,self.payload,'http://manager.internal')
        update(self.store,job['id'],'failed',error='旧模型错误',results=[{'name':'团队/仓库','error':'旧容器错误'}])
        self.store.save_config({'revision':1,'model':'新模型','api_base_url':'http://model.internal/v1','api_key':self.store.seal('新测试密钥')})
        resumed = resume(self.store,job['id'],{'server_id':self.target.id,'config_revision':2})
        self.assertEqual(resumed['model'],'新模型');self.assertEqual(resumed['detail']['results'],[])
        self.assertEqual(resumed['detail']['history'][0]['results'][0]['error'],'旧容器错误')
        with self.store.connect() as c:raw=c.execute('SELECT * FROM setup_jobs WHERE id=?',(job['id'],)).fetchone()
        with patch('ai_review.setup_service.verify_model',side_effect=ReviewError('本轮模型失败')),patch('ai_review.setup_service.ContainerAccess') as transport:
            with self.assertRaises(ReviewError):execute(self.store,raw)
        self.assertFalse(transport.called);self.assertEqual(public_job(self.store,job['id'])['detail']['results'],[])
        self.assertNotIn('新测试密钥',json.dumps(resumed,ensure_ascii=False))

    def test_resume_cannot_change_instance_or_use_stale_revision(self):
        job=submit(self.store,self.payload,'http://manager.internal');update(self.store,job['id'],'failed')
        for data in ({'server_id':99,'config_revision':1},{'server_id':self.target.id,'config_revision':99},{}):
            with self.subTest(输入=data),self.assertRaises(ReviewError):resume(self.store,job['id'],data)
        self.assertEqual(public_job(self.store,job['id'])['status'],'failed')

    def test_latest_setup_is_scoped_to_instance(self):
        first=submit(self.store,self.payload,'http://manager.internal');update(self.store,first['id'],'failed')
        other=GiteaServer(name='Gitea 备',api_token='隔离测试凭据',ssh_user='root',gitea_port=3000,host='backup.internal',gitea_url='http://backup.internal:3000');db.session.add(other);db.session.commit()
        second=submit(self.store,dict(self.payload,server_id=other.id,repositories=[{'server_id':other.id,'repo_id':12,'name':'团队/仓库'}]),'http://manager.internal')
        self.assertEqual(public_job(self.store,server_id=self.target.id)['id'],first['id'])
        self.assertEqual(public_job(self.store,server_id=other.id)['id'],second['id'])

    def test_bare_ip_is_accepted_consistently_and_unsafe_url_rejected(self):
        for raw, wanted in [('172.31.8.55','http://172.31.8.55'),('172.31.8.55:3000','http://172.31.8.55:3000'),('https://gitea.internal/path/','https://gitea.internal/path')]:
            self.assertEqual(gitea_address(raw),wanted)
        for bad in ('http://user:password@gitea.internal','gitea.internal?q=x','gitea.internal/#a','ftp://gitea.internal'):
            with self.assertRaises(ReviewError):gitea_address(bad)
        self.target.gitea_url='172.31.8.55:3000';db.session.commit()
        with patch('routes.ai_review_routes.Gitea') as client:
            client.return_value.request.return_value={'data':[]}
            response=self.client.get('/api/ai-review/catalog?server_id='+str(self.target.id),headers=self.login())
        # 只检查规范化，不访问现场主机。
        self.assertNotIn('请输入完整',response.text)

    def test_check_connection_calls_model_not_only_catalog(self):
        self.store.save_config({'revision':1,'model':'当前模型','api_base_url':'http://model.internal/v1','api_key':self.store.seal('测试模型密钥')})
        with patch('ai_review.setup_service.verify_model',return_value={'listed':True,'message':'实际调用通过'}) as verify:
            response=self.client.post('/api/ai-review/connection-test',json={},headers=self.login())
        self.assertEqual(response.status_code,200);self.assertEqual(verify.call_args.args[1]['model'],'当前模型')
        self.assertEqual(response.json['revision'],2)

    def test_bare_address_repo_reuses_canonical_identity_for_progress(self):
        self.target.gitea_url='gitea.internal:3000';db.session.commit()
        client=Mock();client.prefix='/repos/团队/仓库';client.version.return_value='1.26.4'
        client.request.side_effect=[{'id':8,'login':'测试机器人','is_admin':False},{'id':12,'full_name':'团队/仓库','permissions':{'pull':True}},[]]
        with patch('routes.ai_review_routes.Gitea',return_value=client):
            sid,rid,value=repository_values(self.store,dict(server_id=self.target.id,repo_name='团队/仓库',bot_token='只在测试中使用'))
        self.assertEqual(value['gitea_url'],'http://gitea.internal:3000')
        value['gitea_url']='gitea.internal:3000'
        saved=self.store.save_repo(sid,rid,value)
        self.assertEqual(saved['gitea_url'],'http://gitea.internal:3000')
        from ai_review.progress_assets import build_assets
        matching=[r for r in self.store.repos(public=True) if r['gitea_url']==gitea_address(self.target.gitea_url)]
        self.assertTrue(build_assets(matching).getvalue())

    def test_robots_with_admin_write_or_no_read_are_rejected(self):
        for admin,permissions in ((True,{'pull':True}),(False,{'pull':True,'push':True}),(False,{'pull':True,'admin':True}),(False,{'pull':False})):
            client=Mock();client.prefix='/repos/团队/仓库';client.version.return_value='1.26.4'
            client.request.side_effect=[{'id':8,'login':'测试机器人','is_admin':admin},{'id':12,'full_name':'团队/仓库','permissions':permissions}]
            with patch('routes.ai_review_routes.Gitea',return_value=client),self.assertRaises(ReviewError):
                repository_values(self.store,dict(server_id=self.target.id,repo_name='团队/仓库',bot_token='只在测试中使用',enabled=False))
            client.close.assert_called_once()

    def test_readonly_account_and_write_scoped_token_is_accepted(self):
        client=Mock();client.prefix='/repos/团队/仓库';client.version.return_value='1.26.4';client.request.side_effect=[{'id':8,'login':'测试机器人','is_admin':False},{'id':12,'full_name':'团队/仓库','permissions':{'pull':True,'push':False,'admin':False}},[]]
        with patch('routes.ai_review_routes.Gitea',return_value=client):
            _,_,value=repository_values(self.store,dict(server_id=self.target.id,repo_name='团队/仓库',bot_token='只在测试中使用',enabled=False))
        self.assertEqual(value['bot_name'],'测试机器人');self.assertNotEqual(value['bot_token'],'只在测试中使用')

    def test_readonly_issue_check_does_not_silently_accept_unreachable_api(self):
        client=Mock();client.prefix='/repos/团队/仓库';client.version.return_value='1.26.4';client.request.side_effect=[{'id':8,'login':'测试机器人','is_admin':False},{'id':12,'full_name':'团队/仓库','permissions':{'pull':True}},ReviewError('HTTP 403')]
        with patch('routes.ai_review_routes.Gitea',return_value=client),self.assertRaisesRegex(ReviewError,'write:issue'):
                repository_values(self.store,dict(server_id=self.target.id,repo_name='团队/仓库',bot_token='只在测试中使用'))

    def test_token_scope_probe_rejects_readonly_scope_without_creating_comments(self):
        from ai_review.gitea import Gitea
        client=Gitea('http://legacy.internal','测试令牌','团队/仓库')
        for statuses, valid in [([404,422],True),([403],False),([404,403],False),([404,500],False),([200],False)]:
            responses=[]
            for code in statuses:
                response=Mock(status_code=code);context=Mock();context.__enter__=Mock(return_value=response);context.__exit__=Mock(return_value=False);responses.append(context)
            with patch.object(client.session,'post',side_effect=responses) as post:
                if valid:client.verify_review_scopes()
                else:
                    with self.assertRaises(ReviewError):client.verify_review_scopes()
                for call in post.call_args_list:
                    self.assertIn('/0/',call.args[0]);self.assertEqual(call.kwargs['json'],{});self.assertFalse(call.kwargs['allow_redirects'])
        client.close()


class RuntimeRegressionTests(unittest.TestCase):
    setUp=baseline_store.StoreTests.setUp
    enqueue=baseline_store.StoreTests.enqueue

    def test_stale_setup_cannot_override_later_configuration_in_same_transaction(self):
        current=self.store.repo(self.repo);revision=self.store.config()['revision']
        self.store.save_config({'revision':revision,'enabled':False})
        with self.assertRaisesRegex(ReviewError,'接入期间发生变化'):
            self.store.save_repo(current['server_id'],current['repo_id'],{'revision':current['revision'],'enabled':True},self.repo,required_config_revision=revision)
        self.assertEqual(self.store.repo(self.repo)['revision'],current['revision']);self.assertFalse(self.store.config()['enabled'])

    def test_read_requests_do_not_run_migrations_or_write(self):
        statements=[];original=Store.connect
        def traced(s):
            c=original(s);c.set_trace_callback(lambda sql: statements.append(sql.split()[0].upper()));return c
        with patch.object(Store,'connect',traced):
            again=Store(self.store.path);again.jobs();again.config();public_job(again)
        self.assertFalse(set(statements)&{'CREATE','INSERT','UPDATE','DELETE','ALTER'})

    def test_busy_writer_which_broke_old_version_is_waited_without_replay(self):
        ready=threading.Event();calls=[]
        def slow():
            with self.store.tx() as c:c.execute('UPDATE health SET detail=detail');ready.set();time.sleep(1.3)
        thread=threading.Thread(target=slow);thread.start();ready.wait()
        with self.store.tx() as c:
            calls.append('事务正文');c.execute("INSERT INTO health VALUES ('测试',0,'测试')")
        thread.join();self.assertEqual(calls,['事务正文'])
        with self.store.connect() as c:self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0],5000)

    def test_transaction_body_error_is_not_replayed(self):
        calls=[]
        with self.assertRaises(sqlite3.OperationalError):
            with self.store.tx() as c:
                calls.append('一次');c.execute("INSERT INTO health VALUES ('回退',0,'测试')");raise sqlite3.OperationalError('受控失败')
        self.assertEqual(calls,['一次'])
        with self.store.connect() as c:self.assertIsNone(c.execute("SELECT 1 FROM health WHERE name='回退'").fetchone())

    def test_diagnostic_distinguishes_sqlite_failures_without_sensitive_text(self):
        for code,name in [(5,'REVIEW_DB_BUSY'),(6,'REVIEW_DB_LOCKED'),(13,'REVIEW_DB_FULL'),(8,'REVIEW_DB_READONLY'),(10,'REVIEW_DB_IO'),(11,'REVIEW_DB_CORRUPT')]:
            error=sqlite3.OperationalError('秘密内容不得进入日志');error.sqlite_errorcode=code
            with self.assertLogs(level='ERROR') as logs:result=describe(error,{'id':'测试任务'},'写单元检查点')
            self.assertEqual(result['error_code'],name);self.assertNotIn('秘密内容',''.join(logs.output))

    def test_creation_order_ignores_update_time_and_has_stable_tie_break(self):
        ids=[self.store.enqueue(self.repo,i,{'head':'a','base':'b'}) for i in range(1,5)]
        with self.store.tx() as c:
            for i,ident in enumerate(ids):c.execute('UPDATE jobs SET created=?,updated=? WHERE id=?',(100 if i<3 else 200,900-i,ident))
        expected=[ids[3],*sorted(ids[:3],reverse=True)]
        self.assertEqual([r['id'] for r in self.store.jobs()['items']],expected)
        self.assertNotIn('snapshot',self.store.jobs()['items'][0]);self.assertNotIn('result',self.store.jobs()['items'][0])

    def test_ninety_units_continue_batches_with_concurrent_store_writers(self):
        self.enqueue();job=self.store.claim();cfg=dict(self.store.config(),max_calls=8)
        items=[{'path':f'src/file{i//9}.c','scope':{'name':f'f{i}'},'anchors':[]} for i in range(90)]
        calls=[];stop=threading.Event();errors=[]
        def writer(index):
            try:
                other=Store(self.store.path)
                for n in range(120):
                    other.health('并发'+str(index),str(n));other.jobs()
                    if n%8==0:other.heartbeat(job['id'],job['owner'])
            except Exception as exc:errors.append(type(exc).__name__)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures=[pool.submit(writer,i) for i in range(4)]
            result=review(self.store,job,cfg,{'coverage':{'skipped':[],'files_total':10,'files_reviewed':10}},items,caller=lambda *args:(calls.append(args[-1]['scope']['name']) or {'findings':[]}))
            for f in futures:f.result()
        actual=self.store.job(job['id']);self.assertFalse(errors);self.assertEqual(len(calls),90)
        self.assertEqual(actual['units_completed'],90);self.assertGreater(actual['batch_index'],1);self.assertTrue(result['coverage']['complete'])
        before=len(calls)
        review(self.store,job,cfg,{'coverage':{'skipped':[]}},items,caller=lambda *args:calls.append('不应重复'))
        self.assertEqual(len(calls),before)

    def test_unknown_phase_does_not_automatically_repeat_model_call(self):
        self.enqueue();job=self.store.claim();items=[{'path':'code.c','scope':{'name':'f'},'anchors':[]}];calls=[]
        def failed(*args):calls.append(1);raise ReviewError('模型连接断开')
        for _ in range(2):
            with self.assertRaises(ReviewError):review(self.store,job,self.store.config(),{'coverage':{'skipped':[]}},items,caller=failed)
        self.assertEqual(len(calls),1)

    def test_interrupted_large_plan_reports_partial_coverage_immediately(self):
        self.enqueue();job=self.store.claim();items=[{'path':'code.c','scope':{'name':f'f{i}'},'anchors':[]} for i in range(90)]
        calls=[]
        def failed(*args):
            calls.append(1)
            if len(calls)==21:raise ReviewError('测试进程终止')
            return {'findings':[]}
        review(self.store,job,self.store.config(),{'coverage':{'complete':True,'skipped':[]}},items,caller=failed)
        for evidence in (True,False):
            actual=self.store.job(job['id'],evidence=evidence)
            self.assertEqual(actual['units_completed'],89);self.assertFalse(actual['result']['coverage']['complete'])
            self.assertIn('1/90',actual['result']['coverage']['skipped'][-1]['reason'])

    def test_more_than_four_headers_are_loaded_once_per_version(self):
        old=''.join(f'#include "h{i}.h"\n' for i in range(8))+'int f(void){return 0;}\nint g(void){return 0;}\n'
        _,_,snapshot=example(old,old.replace('return 0','return 1'));snapshot.update(merge_base='a'*40,head='b'*40)
        client=Mock();client.file.return_value=b'extern int value;'
        items=work_items(snapshot,client)
        self.assertEqual(client.file.call_count,16);self.assertEqual(len(items),2)
        self.assertFalse(any('未读取本地头文件' in g for i in items for g in i['context_gaps']))

    def test_large_function_processes_changed_anchors_and_discloses_missing_context(self):
        old='int huge(int x) {\n'+''.join(f'  x += {i};\n' for i in range(7000))+'return x;\n}\n'
        new=old.replace('x += 50;', 'x -= 50;').replace('x += 6500;', 'x -= 6500;')
        _,_,snapshot=example(old,new);items=work_items(snapshot)
        self.assertTrue(items);self.assertEqual({a['id'] for i in items for a in i['anchors']},set(snapshot['anchors']))
        self.assertTrue(all(len(json.dumps(i,ensure_ascii=False))<52000 for i in items));self.assertFalse(snapshot['coverage']['complete'])


class PersistenceRegressionTests(unittest.TestCase):
    setUp=baseline_store.StoreTests.setUp
    enqueue=baseline_store.StoreTests.enqueue

    def test_completed_plan_progress_survives_reclaim(self):
        from ai_review.review_state import prepare, checkpoint
        self.enqueue();job=self.store.claim();items=[{'path':'代码.c','scope':{'name':'函数'}}]
        row=prepare(self.store,job,items)[0];checkpoint(self.store,job,row['id'],'done',value={})
        with self.store.tx() as c:c.execute('UPDATE jobs SET lease=0 WHERE id=?',(job['id'],))
        resumed=self.store.claim();prepare(self.store,resumed,items)
        self.assertEqual(self.store.job(job['id'])['units_completed'],1)

    def test_pruning_drops_source_units_but_keeps_unknown_publication(self):
        from ai_review.review_state import prepare
        self.enqueue();job=self.store.claim();prepare(self.store,job,[{'path':'私有代码.c','scope':{'name':'函数'}}])
        self.store.update(job['id'],job['owner'],snapshot={'源码':'仅测试'},result={});self.store.finish(job['id'],job['owner'],'publish_unknown')
        with self.store.tx() as c:c.execute('UPDATE jobs SET updated=?',(time.time()-40*86400,))
        self.store.prune()
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM review_units').fetchone()[0],1)
        with self.store.tx() as c:c.execute("UPDATE jobs SET status='failed'")
        self.store.prune()
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM review_units').fetchone()[0],0)

    def test_feedback_ids_and_archived_context_bind_to_unit(self):
        from ai_review.dataset import archive,finding_id
        self.enqueue();job=self.store.claim()
        rows=[dict(candidate_id='同一个模型候选编号',unit_id=str(i),path='code.c',scope='同一大函数',quality_version=3) for i in range(2)]
        self.assertNotEqual(finding_id(job,rows[0]),finding_id(job,rows[1]))
        evidence=[{'unit_id':str(i),'input':{'片段':i}} for i in range(2)]
        archive(self.store,job,dict(head='a',base='b',merge_base='b'),dict(findings=rows,evidence=evidence))
        with self.store.connect() as c:values=[json.loads(r[0]) for r in c.execute('SELECT value FROM review_finding_archive')]
        self.assertEqual({v['context']['片段'] for v in values},{0,1})

    def test_old_task_does_not_show_new_generation_progress_failure(self):
        old=self.store.enqueue(self.repo,1,{'head':'旧'});new=self.store.enqueue(self.repo,1,{'head':'新'})
        with self.store.tx() as c:c.execute("UPDATE pr_state SET progress_error='新任务的错误'")
        self.assertEqual(self.store.job(old)['progress']['sync_error'],'')
        self.assertEqual(self.store.job(new)['progress']['sync_error'],'新任务的错误')


import test_ai_review_publication as publication_baseline
class PublicationLimitRegressionTests(unittest.TestCase):
    setUp=publication_baseline.PublicationTests.setUp

    def test_publication_limit_keeps_all_findings_and_frozen_recovery(self):
        from ai_review.publish import Publisher
        from ai_review.review_state import prepare,checkpoint
        import copy
        items=[{'path':'code.c','scope':{'name':str(i)}} for i in range(4)]
        rows=prepare(self.store,self.job,items)
        for i,row in enumerate(rows):
            finding=dict(copy.deepcopy(self.result['findings'][0]),fingerprint='意见'+str(i),title='意见'+str(i),unit_id=row['id'])
            checkpoint(self.store,self.job,row['id'],'done',value={'findings':[finding]})
        result=self.store.job(self.job['id'])['result'];result['publication_limit']=2
        self.store.update(self.job['id'],self.job['owner'],result=result)
        publisher=Publisher(self.store,self.client,self.job,self.store.repo(self.repo),self.snapshot,result)
        publisher.publish()
        saved=self.store.job(self.job['id']);self.assertEqual(len(saved['result']['findings']),4)
        self.assertEqual(len(saved['result']['publication']['findings']),4)
        restored=Publisher(self.store,self.client,self.job,self.store.repo(self.repo),self.snapshot,saved['result'])
        before=len([x for x in self.client.calls if x[0]=='POST'])
        restored.publish(recovery=True)
        self.assertEqual(len([x for x in self.client.calls if x[0]=='POST']),before)
        self.assertEqual(len(self.client.timeline),2)
        self.assertTrue(all(p['state']=='confirmed' for p in saved['result']['publication_pages']))


if __name__=='__main__':unittest.main()
