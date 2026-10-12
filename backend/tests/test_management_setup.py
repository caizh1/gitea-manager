"""接入向导的身份、CSRF、指纹固定和凭据不回传边界。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from flask import Flask, g
from tests.access_helpers import init_test_auth as init_auth, login_platform
from models import db, GiteaServer
from routes.management_routes import management_bp
from routes.server_routes import server_to_dict
import paramiko
import time


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'SSH_KNOWN_HOSTS':str(self.root/'known_hosts'),'MANAGEMENT_DIAGNOSTICS':str(self.root/'diagnostics')})
        self.env.start();self.addCleanup(self.env.stop)
        self.app=Flask(__name__);self.app.secret_key='隔离测试会话'
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite://',TESTING=True)
        db.init_app(self.app);init_auth(self.app);self.app.register_blueprint(management_bp,url_prefix='/api/management')
        self.context=self.app.app_context();self.context.push();self.addCleanup(self.context.pop)
        db.create_all();self.addCleanup(db.session.remove)
        self.client=self.app.test_client()

    def login(self):
        g.pop('_login_user',None)
        login_platform(self.client)
        token=self.client.get('/api/management/status').json['csrf']
        return {'X-Management-CSRF':token}

    def test_login_and_csrf_checked_before_docker(self):
        with patch('routes.management_routes.docker.from_env') as docker:
            self.assertEqual(self.client.post('/api/management/discover',json={'mode':'local'}).status_code,401)
            self.login()
            self.assertEqual(self.client.post('/api/management/discover',json={'mode':'local'}).status_code,403)
            docker.assert_not_called()

    def test_changed_host_key_cannot_be_replaced(self):
        headers=self.login()
        for index in range(2):
            key=paramiko.RSAKey.generate(1024)
            with self.client.session_transaction() as s:
                s['ssh_pending']={'host':'server.internal','port':22,'type':key.get_name(),'key':key.get_base64(),'fingerprint':'测试指纹'+str(index),'time':time.time()}
            response=self.client.post('/api/management/ssh-trust',headers=headers,json={'fingerprint':'测试指纹'+str(index)})
            self.assertEqual(response.status_code,200 if index==0 else 409)
            if index==0:before=(self.root/'known_hosts').read_bytes()
        self.assertEqual(before,(self.root/'known_hosts').read_bytes())
        self.assertEqual((self.root/'known_hosts').stat().st_mode & 0o777,0o600)

    def test_local_failure_stays_local_and_has_downloadable_evidence(self):
        headers=self.login()
        with patch('routes.management_routes.docker.from_env',side_effect=RuntimeError('本机 Docker 权限拒绝')),patch('routes.management_routes.SSHService') as ssh:
            response=self.client.post('/api/management/discover',json={'mode':'local'},headers=headers)
            self.assertEqual(response.status_code,400);ssh.assert_not_called()
        identifier=response.json['diagnostic_id']
        result=self.client.get('/api/management/diagnostics/operation/'+identifier)
        self.assertEqual(result.status_code,200)
        self.assertIn('Docker 权限拒绝',response.json['error'])
        steps=self.client.get('/api/management/diagnostics/operation/'+identifier+'/steps').json['events']
        self.assertTrue(any(x['状态']=='失败' for x in steps))

    def test_api_token_never_serialized(self):
        row=GiteaServer(name='隔离实例',host='127.0.0.1',ssh_user='root',gitea_port=3000,gitea_url='http://127.0.0.1:3000',api_token='仅测试秘密')
        db.session.add(row);db.session.commit()
        self.assertNotIn('仅测试秘密',json.dumps(server_to_dict(row),ensure_ascii=False))

if __name__=='__main__':unittest.main()
