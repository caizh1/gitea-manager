"""管理员原始诊断状态、鉴权绑定与流式下载回归。"""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from flask import Flask

from tests.access_helpers import init_test_auth as init_auth, login_platform
from ai_review.broker import create_broker
from ai_review.raw_diagnostics import metadata, signature
from ai_review.session_events import _redact, create_session
from ai_review.store import Store
from routes.ai_review_routes import ai_review_bp


class ArchiveResponse:
    status_code=200
    def __init__(self,body=None,job='',session=''):
        body = body or b'PK\x03\x04'+'原始模型响应'.encode()
        self.body=body;self.content=body;self.closed=False
        self.headers={'Content-Type':'application/zip','Content-Length':str(len(body)),
                      'X-Diagnostics-Job-ID':job,'X-Diagnostics-Session-ID':session,
                      'X-Diagnostics-Truncated':'false'}
    def iter_content(self,_):
        yield self.body[:4];yield self.body[4:]
    def json(self):return {'error':'不可用'}
    def close(self):self.closed=True


class RawDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);key=root/'master';key.write_bytes(Fernet.generate_key())
        executor=root/'executor';executor.write_text('executor-test-token-'+'x'*32)
        env=patch.dict(os.environ,{'AI_REVIEW_MASTER_KEY_FILE':str(key),
            'AI_REVIEW_EXECUTOR_TOKEN_FILE':str(executor)})
        env.start();self.addCleanup(env.stop)
        self.store=Store(root/'review.db')
        self.store.save_config({'revision':1,'enabled':True,'debounce_seconds':0,
                                'model':'测试模型','engine':'opencode_native'})
        repo=self.store.save_repo(1,7,{'enabled':True,'bot_token':self.store.seal('凭据'),
            'webhook_secret':self.store.seal('签名'),'repo_name':'test/code','bot_id':9})['id']
        self.ident=self.store.enqueue(repo,1,{'head':'a'*40,'base':'b'*40})
        self.job=self.store.claim()
        self.session,_,_=create_session(self.store,self.ident,self.job['owner'],time.time()+600,'/work')
        status={'status':'available','available':True,'truncated':False,'bytes':123,
                'original_bytes':123,'retention_expires_at':time.time()+3600,'sha256':'a'*64,
                'job_id':self.ident,'session_id':self.session}
        self.store.update(self.ident,self.job['owner'],raw_diagnostics=status)
        self.store.finish(self.ident,self.job['owner'],'failed','测试终态')

    def login_app(self,login=True):
        app=Flask(__name__);app.config.update(SECRET_KEY='测试',AI_REVIEW_DB=str(self.store.path))
        init_auth(app);app.register_blueprint(ai_review_bp,url_prefix='/api/ai-review')
        client=app.test_client()
        if login:
            login_platform(client)
        return client

    def test_metadata_is_bound_and_malformed_values_are_rejected(self):
        value=metadata({'status':'partial','available':True,'truncated':True,'bytes':10,
                        'original_bytes':20,'error':'storage_limit','sha256':'b'*64},
                       self.ident,self.session)
        self.assertEqual(value['status'],'partial');self.assertEqual(value['original_bytes'],20)
        self.assertIsNone(metadata({'available':True,'job_id':'其他任务'},self.ident,self.session))
        malformed=metadata({'status':{},'available':'yes','bytes':{},'retention_expires_at':[],
                            'error':'../../secret'},self.ident,self.session)
        self.assertEqual(malformed,{'available':False,'truncated':False,
                                    'job_id':self.ident,'session_id':self.session})

    def test_version_ten_database_migrates_metadata_column_without_losing_job(self):
        with self.store.tx() as connection:
            connection.execute('PRAGMA user_version=10')
            connection.execute('ALTER TABLE jobs DROP COLUMN raw_diagnostics')
        reopened=Store(self.store.path)
        with reopened.connect() as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0],13)
            self.assertIn('raw_diagnostics',{row[1] for row in connection.execute('PRAGMA table_info(jobs)')})
        self.assertEqual(reopened.job(self.ident,evidence=False)['status'],'failed')

    def test_numeric_token_budgets_remain_visible_but_credentials_are_redacted(self):
        self.assertEqual(_redact(32768,'model_context_tokens'),32768)
        self.assertEqual(_redact('sk-private','model_context_tokens'),'[已脱敏]')
        self.assertEqual(_redact('Bearer abcdefghijk','Authorization'),'[已脱敏]')

    def test_worker_proxy_requires_hmac_job_session_binding_and_fixed_path(self):
        client=create_broker(self.store).test_client();stamp=int(time.time())
        url=f'/internal/raw-diagnostics/{self.ident}/{self.session}/archive'
        self.assertEqual(client.get(url).status_code,401)
        bad={'X-Raw-Diagnostics-Time':'9'*1000,'X-Raw-Diagnostics-Signature':'非ASCII'}
        self.assertEqual(client.get(url,headers=bad).status_code,401)
        wrong='rvw_'+'x'*32
        headers={'X-Raw-Diagnostics-Time':str(stamp),
                 'X-Raw-Diagnostics-Signature':signature('archive',self.ident,wrong,stamp)}
        self.assertEqual(client.get(f'/internal/raw-diagnostics/{self.ident}/{wrong}/archive',headers=headers).status_code,403)
        self.assertEqual(client.get('/internal/raw-diagnostics/../../etc/passwd/archive').status_code,404)
        headers={'X-Raw-Diagnostics-Time':str(stamp),
                 'X-Raw-Diagnostics-Signature':signature('archive',self.ident,self.session,stamp)}
        upstream=ArchiveResponse(job=self.ident,session=self.session)
        with patch('requests.Session.get',return_value=upstream):
            response=client.get(url,headers=headers)
        self.assertEqual(response.status_code,200);self.assertEqual(response.data,upstream.body)
        oversized=ArchiveResponse(job=self.ident,session=self.session)
        oversized.headers['Content-Length']=str(261*1024*1024)
        with patch('requests.Session.get',return_value=oversized):
            self.assertEqual(client.get(url,headers=headers).status_code,502)
        broken=ArchiveResponse(job=self.ident,session=self.session);broken.status_code=503
        broken.raw=Mock();broken.raw.read.side_effect=OSError('读取中断')
        with patch('requests.Session.get',return_value=broken):
            self.assertEqual(client.get(url,headers=headers).status_code,503)
        self.assertTrue(broken.closed)
        old_stamp=stamp-120
        stale={'X-Raw-Diagnostics-Time':str(old_stamp),
               'X-Raw-Diagnostics-Signature':signature('archive',self.ident,self.session,old_stamp)}
        self.assertEqual(client.get(url,headers=stale).status_code,401)

    def test_admin_route_requires_login_supports_head_and_streams_worker_archive(self):
        app_client=self.login_app();url=f'/api/ai-review/tasks/{self.ident}/raw-diagnostics'
        self.assertEqual(self.login_app(False).get(url).status_code,401)
        lock=self.store.lock('原始诊断下载',blocking=False);lock.__enter__()
        try:self.assertEqual(app_client.head(url).status_code,429)
        finally:lock.__exit__(None,None,None)
        worker=ArchiveResponse(job=self.ident,session=self.session)
        with patch('requests.Session.request',return_value=worker):
            head=app_client.head(url)
            self.assertEqual(head.status_code,200)
            self.assertEqual(int(head.headers['Content-Length']),len(worker.body))
            head.close()
        interrupted=ArchiveResponse(job=self.ident,session=self.session)
        with patch('requests.Session.request',return_value=interrupted):
            streaming=app_client.get(url,buffered=False)
            self.assertEqual(streaming.status_code,200)
            iterator=iter(streaming.response);next(iterator)
            if hasattr(iterator,'close'):iterator.close()
            streaming.close()
        self.assertTrue(interrupted.closed)
        with patch('requests.Session.request',return_value=worker):
            response=app_client.get(url)
        self.assertEqual(response.status_code,200);self.assertEqual(response.data,worker.body)
        self.assertIn(self.ident,response.headers['Content-Disposition'])
        response.close()
        broken=ArchiveResponse(job=self.ident,session=self.session);broken.status_code=503
        broken.raw=Mock();broken.raw.read.side_effect=OSError('读取中断')
        with patch('requests.Session.request',return_value=broken):
            self.assertEqual(app_client.get(url).status_code,503)
        self.assertTrue(broken.closed)
        other=self.store.enqueue(self.job['repository'],2,{'head':'c'*40,'base':'d'*40})
        self.assertEqual(app_client.get(f'/api/ai-review/tasks/{other}/raw-diagnostics').status_code,404)
        with self.store.tx() as connection:
            connection.execute("UPDATE jobs SET raw_diagnostics=json_set(raw_diagnostics,'$.retention_expires_at',?) WHERE id=?",(time.time()-1,self.ident))
        self.assertEqual(app_client.get(url).status_code,410)
