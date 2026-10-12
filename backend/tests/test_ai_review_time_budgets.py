"""时间预算冻结、执行版本隔离、首个错误与新旧配置兼容回归。"""
import concurrent.futures
import json
import time
import unittest
from unittest.mock import patch, Mock

from tests import test_ai_review as foundation
from ai_review.budgets import model_timeout
from ai_review.common import ReviewError, Stale
from ai_review.engine import capability
from ai_review.broker import create_broker
from ai_review.model_transport import ModelTransportError
from ai_review.review_state import prepare, begin_phase


class TimeBudgetTests(unittest.TestCase):
    setUp = foundation.StoreTests.setUp
    enqueue = foundation.StoreTests.enqueue
    def change(self, **values):
        return self.store.save_config(dict(values, revision=self.store.config()['revision']))

    def test_old_configuration_defaults_do_not_overwrite_batch_budget(self):
        with self.store.tx() as c:
            old = json.loads(c.execute('SELECT value FROM settings').fetchone()[0])
            old.pop('model_request_timeout_seconds', None); old.pop('execution_revision', None)
            old['task_timeout_seconds'] = 777
            c.execute('UPDATE settings SET value=?', (json.dumps(old),))
        config = self.store.config()
        self.assertEqual(config['model_request_timeout_seconds'], 600)
        self.assertEqual(config['task_timeout_seconds'], 777)
        self.assertEqual(config['execution_revision'], config['revision'])

    def test_budget_edit_preserves_running_queued_and_finished_pr_identity(self):
        running_id = self.enqueue(); running = self.store.claim()
        queued_id = self.store.enqueue(self.repo, 2, {'head':'c','base':'b'})
        original = self.store.job_config(running_id)
        changed = self.change(task_timeout_seconds=2400, model_request_timeout_seconds=900)
        self.store.active(running_id, running['owner'])
        self.assertEqual(self.store.job(running_id)['status'], 'running')
        self.assertEqual(self.store.job(queued_id)['status'], 'queued')
        self.assertEqual(self.store.job_config(running_id), original)
        self.assertEqual(self.store.job_config(queued_id)['model_request_timeout_seconds'], 600)
        self.assertIsNone(self.enqueue())
        self.assertIsNone(self.store.enqueue(self.repo, 2, {'head':'c','base':'b'}))
        self.store.finish(running_id, running['owner'], 'failed', '保留原失败')
        self.assertIsNone(self.enqueue())
        new_id = self.store.enqueue(self.repo, 3, {'head':'d','base':'b'})
        self.assertEqual(self.store.job_config(new_id)['model_request_timeout_seconds'], 900)
        self.assertEqual(self.store.job_config(new_id)['task_timeout_seconds'], 2400)
        self.assertEqual(self.store.job(new_id)['config_revision'], changed['revision'])

    def test_snapshot_refresh_does_not_rewrite_signature_to_budget_edit_version(self):
        self.change(model_request_timeout_seconds=900)
        ident = self.store.enqueue(self.repo, 1, {'manual':'显式创建'}, manual=True)
        job = self.store.claim()
        self.store.update(ident, job['owner'], snapshot={'head':'a','base':'b'})
        self.assertIsNone(self.enqueue())
        self.store.finish(ident, job['owner'], 'failed', '保留')
        self.change(task_timeout_seconds=1800)
        self.assertIsNone(self.enqueue())

    def test_sensitive_config_and_repo_changes_still_fence_old_jobs(self):
        for field, value in [('model','另一个模型'), ('api_key',self.store.seal('新密钥')), ('enabled',False)]:
            with self.subTest(字段=field):
                self.change(enabled=True)
                ident = self.store.enqueue(self.repo, 20, {'manual':field}, manual=True)
                job = self.store.claim()
                self.change(**{field:value})
                with self.assertRaises(Stale):self.store.active(ident, job['owner'])
                with self.assertRaises(Stale):self.store.job_config(ident)
        self.change(enabled=True)
        ident = self.store.enqueue(self.repo, 21, {'head':'a','base':'b'})
        job = self.store.claim(); repo = self.store.repo(self.repo)
        self.store.save_repo(repo['server_id'], repo['repo_id'], dict(repo, revision=repo['revision'], enabled=False), self.repo)
        with self.assertRaises(Stale):self.store.active(ident, job['owner'])

    def test_concurrent_edits_have_one_winner_and_keep_execution_revision(self):
        current = self.store.config()
        def save(value):
            try:
                self.store.save_config({'revision':current['revision'],'model_request_timeout_seconds':value})
                return True
            except ReviewError:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(save, [800,900])), 1)
        self.assertEqual(self.store.config()['execution_revision'], current['execution_revision'])

    def test_effective_deadline_and_batch_switch_reserve_complete_call(self):
        self.assertEqual(model_timeout({'task_timeout_seconds':60,'model_request_timeout_seconds':600}),20)
        self.assertEqual(model_timeout({'task_timeout_seconds':1200,'model_request_timeout_seconds':600}),600)
        self.assertEqual(model_timeout({'task_timeout_seconds':3600,'model_request_timeout_seconds':3600}),3560)
        self.enqueue(); job = self.store.claim(); config = self.store.job_config(job['id'])
        rows = prepare(self.store, job, [{'path':'x.c'}])
        first = begin_phase(self.store, job, config, rows[0]['id'], 'analyzing')
        with self.store.tx() as c:
            c.execute('UPDATE jobs SET batch_started=? WHERE id=?',(time.time()-600,job['id']))
        second = begin_phase(self.store, job, config, rows[0]['id'], 'analyzing')
        self.assertEqual(second, first+1)

    def test_first_timeout_is_preserved_after_cli_authorization_rejection(self):
        self.enqueue(); job = self.store.claim(); token = capability(self.store, job, '测试模型')
        client = create_broker(self.store).test_client()
        payload = {'model':'测试模型','messages':[{'role':'user','content':'私有测试源码'}]}
        with patch('ai_review.broker.endpoint', return_value='http://model.invalid/v1'), \
             patch.object(self.store, 'unseal', return_value='私有 Key'), \
             patch('ai_review.broker.fetch_model', side_effect=ModelTransportError('Timeout')) as transport:
            a = client.post('/v1/chat/completions', json=payload, headers={'Authorization':'Bearer '+token})
            b = client.post('/v1/chat/completions', json=payload, headers={'Authorization':'Bearer '+token})
        self.assertEqual((a.status_code,b.status_code),(502,403));transport.assert_called_once()
        actual = self.store.job(job['id'])
        self.assertEqual(actual['diagnostic']['error_code'], 'MODEL_TOTAL_TIMEOUT')
        self.assertIn('600 秒',actual['error']);self.assertNotIn('403',actual['error'])
        self.assertNotIn(token,json.dumps(actual['diagnostic']))

    def test_broker_uses_frozen_deadline_and_distinguishes_upstream_403(self):
        self.enqueue(); job = self.store.claim(); token = capability(self.store, job, '测试模型')
        self.change(model_request_timeout_seconds=900)
        client = create_broker(self.store).test_client()
        def upstream(*args, **kwargs):
            self.assertEqual(kwargs['timeout'],600)
            kwargs['diagnostics']['upstream_http_status'] = 403
            return 403,b''
        with patch('ai_review.broker.endpoint',return_value='http://model.invalid/v1'), \
             patch.object(self.store,'unseal',return_value='私有 Key'), patch('ai_review.broker.fetch_model',side_effect=upstream):
            response=client.post('/v1/chat/completions',json={'model':'测试模型','messages':[{'role':'user','content':'合成'}]},headers={'Authorization':'Bearer '+token})
        self.assertEqual(response.status_code,502)
        diagnostic=self.store.job(job['id'])['diagnostic']
        self.assertEqual(diagnostic['error_code'],'MODEL_HTTP_ERROR')
        self.assertEqual(diagnostic['upstream_http_status'],403)

    def test_api_auth_csrf_validation_persistence_and_conflict(self):
        from flask import Flask
        from tests.access_helpers import init_test_auth as init_auth, login_platform
        from routes.ai_review_routes import ai_review_bp
        self.change(model='test-model',api_base_url='http://model.invalid/v1',api_key=self.store.seal('合成 Key'))
        app=Flask('预算接口验收');app.secret_key='本机测试会话';app.config['AI_REVIEW_DB']=str(self.store.path)
        init_auth(app);app.register_blueprint(ai_review_bp,url_prefix='/api/ai-review')
        client=app.test_client()
        self.assertEqual(client.get('/api/ai-review/config').status_code,401)
        login_platform(client)
        initial=client.get('/api/ai-review/config').json['config']
        self.assertEqual(initial['model_request_timeout_seconds'],600)
        self.assertEqual(client.put('/api/ai-review/config',json={'revision':initial['revision'],'model_request_timeout_seconds':900}).status_code,403)
        headers={'X-Review-CSRF':client.get('/api/ai-review/csrf').json['token']}
        self.assertEqual(client.put('/api/ai-review/config',json={},headers={'X-Review-CSRF':'é'}).status_code,403)
        for field in ('task_timeout_seconds','model_request_timeout_seconds'):
            for invalid in (59,3601,True,600.5,'600',None):
                with self.subTest(字段=field,值=invalid):
                    response=client.put('/api/ai-review/config',json={'revision':initial['revision'],field:invalid},headers=headers)
                    self.assertEqual(response.status_code,400)
        with patch('routes.ai_review_routes.endpoint',return_value='http://model.invalid/v1'):
            response=client.put('/api/ai-review/config',json={'revision':initial['revision'],'model_request_timeout_seconds':900},headers=headers)
            self.assertEqual(response.status_code,200,response.json)
            self.assertEqual(response.json['model_request_timeout_seconds'],900)
            self.assertEqual(client.put('/api/ai-review/config',json={'revision':initial['revision'],'model_request_timeout_seconds':800},headers=headers).status_code,400)
            for value in (60,3600):
                revision=client.get('/api/ai-review/config').json['config']['revision']
                response=client.put('/api/ai-review/config',json={'revision':revision,'task_timeout_seconds':value,'model_request_timeout_seconds':value},headers=headers)
                self.assertEqual(response.status_code,200,response.json)
        with patch('routes.ai_review_routes.endpoint',return_value='http://model.invalid/v1'):
            for field, bounds in (('model_max_output_tokens',(0,52144)),('model_response_max_mib',(1,64))):
                for invalid in (bounds[0]-1,bounds[1]+1,True,8.5,'8',None):
                    revision=self.store.config()['revision']
                    self.assertEqual(client.put('/api/ai-review/config',json={'revision':revision,field:invalid},headers=headers).status_code,400)
                for bound in bounds:
                    revision=self.store.config()['revision']
                    response=client.put('/api/ai-review/config',json={'revision':revision,field:bound},headers=headers)
                    self.assertEqual(response.status_code,200,response.json)
                    self.assertEqual(response.json[field],bound)
                self.assertEqual(client.put('/api/ai-review/config',json={'revision':revision,field:bounds[0]},headers=headers).status_code,400)
        reopened=type(self.store)(self.store.path).config()
        self.assertEqual(reopened['model_max_output_tokens'],52144)
        self.assertEqual(reopened['model_response_max_mib'],64)
        self.assertEqual(reopened['task_timeout_seconds'],3600)
        self.assertEqual(reopened['model_request_timeout_seconds'],3600)
        revision=reopened['revision']
        self.assertEqual(client.put('/api/ai-review/config',json={'revision':revision,'execution_revision':1},headers=headers).status_code,400)

    def test_executor_bad_response_or_disconnect_preserves_first_gateway_failure(self):
        import hashlib
        import requests
        from ai_review.engine import invoke
        for mode in ('非JSON错误页', '执行器等待超时', '后续403'):
            with self.subTest(场景=mode):
                ident=self.store.enqueue(self.repo,30,{'manual':mode},manual=True)
                job=self.store.claim(); config=self.store.job_config(ident)
                session=Mock()
                def post(*args,**kwargs):
                    call_id=hashlib.sha256(kwargs['json']['capability'].encode()).hexdigest()[:24]
                    self.store.update(ident,job['owner'],diagnostic={'call_id':call_id,'status':'failed',
                        'error_code':'MODEL_TOTAL_TIMEOUT','message':'首次总超时','elapsed_seconds':600,'timeout_seconds':600})
                    if mode=='执行器等待超时':raise requests.ReadTimeout()
                    response=Mock(status_code=502,content=b'<html>')
                    if mode=='非JSON错误页':response.json.side_effect=ValueError()
                    else:response.json.return_value={'error_code':'cli_exit','diagnostic':{'http_status':403}}
                    return response
                session.post.side_effect=post
                with patch('ai_review.engine.requests.Session',return_value=session), patch('ai_review.engine.secret_file',return_value='合成令牌'):
                    with self.assertRaises(ReviewError) as caught:invoke(self.store,job,config,'合成指令',{})
                self.assertEqual(caught.exception.error_code,'MODEL_TOTAL_TIMEOUT')
                self.assertEqual(str(caught.exception),'首次总超时')
                session.post.assert_called_once();session.close.assert_called_once()
                with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM capabilities').fetchone()[0],0)
                if mode=='后续403':self.assertEqual(self.store.job(ident)['diagnostic']['secondary_http_status'],403)
                self.store.finish(ident,job['owner'],'failed','首次总超时')
