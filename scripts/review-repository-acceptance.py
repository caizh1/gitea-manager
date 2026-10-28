#!/usr/bin/env python3
"""新建独立 Gitea 容器，验收接入移除；不访问用户已有实例。"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import uuid

ROOT = Path('/app') if '--inside' in sys.argv else Path(__file__).resolve().parents[1]


def inside():
    import tempfile
    import time
    from types import SimpleNamespace
    from unittest.mock import patch
    import requests
    from cryptography.fernet import Fernet
    from flask import Flask
    from ai_review.store import Store
    from routes.ai_review_routes import ai_review_bp
    from tests.access_helpers import init_test_auth, login_platform
    from access.control import install

    values = json.load(sys.stdin)
    endpoint = 'http://gitea:3000'
    client = requests.Session(); client.trust_env = False
    def api(method, path, data=None, **kwargs):
        response = client.request(method, endpoint + '/api/v1' + path, json=data, timeout=10, **kwargs)
        if response.status_code >= 300:
            raise RuntimeError('隔离 Gitea 接口失败：' + method + ' ' + path + '，状态 ' + str(response.status_code))
        return response.json() if response.content else None
    token = api('POST', '/users/' + values['username'] + '/tokens',
                {'name': 'isolated-removal', 'scopes': ['all']}, auth=(values['username'], values['password']))['sha1']
    client.headers['Authorization'] = 'token ' + token
    repo = api('POST', '/user/repos', {'name': 'removal-test', 'private': True, 'auto_init': True})
    prefix = '/repos/' + repo['full_name']
    with tempfile.TemporaryDirectory(prefix='review-removal-') as folder:
        key = Path(folder) / 'master.key'; key.write_bytes(Fernet.generate_key())
        os.environ.update(AI_REVIEW_MASTER_KEY_FILE=str(key), AI_REVIEW_GITEA_HOSTS='gitea')
        state = Store(Path(folder) / 'review.db')
        state.save_config({'enabled': True, 'revision': 1, 'model': '隔离测试模型'})
        config = dict(enabled=True, server_name='隔离 Gitea', repo_name=repo['full_name'], gitea_url=endpoint,
                      bot_token=state.seal('隔离令牌'), webhook_secret=state.seal('隔离签名'), hook_status='未安装')
        saved = state.save_repo(1, repo['id'], config)
        callback = 'http://manager.invalid/api/ai-review/webhooks/' + str(saved['id'])
        hook = api('POST', prefix + '/hooks', {'type': 'gitea', 'active': True, 'events': ['pull_request'],
                   'config': {'url': callback, 'content_type': 'json', 'secret': 'isolated-secret'}})
        other = api('POST', prefix + '/hooks', {'type': 'gitea', 'active': True, 'events': ['pull_request'],
                    'config': {'url': 'http://another-service.invalid/hook', 'content_type': 'json'}})
        state.repo_status(saved['id'], hook_id=hook['id'], webhook_url=callback, hook_status='已安装并回读验证')
        job = state.enqueue(saved['id'], 1, {'head': 'a', 'base': 'b'})
        app = Flask(__name__); app.secret_key = secrets.token_urlsafe(32)
        app.config.update(AI_REVIEW_DB=str(state.path), TESTING=True)
        init_test_auth(app); app.register_blueprint(ai_review_bp, url_prefix='/api/ai-review'); install(app)
        web = app.test_client(); csrf = login_platform(web)
        target = SimpleNamespace(gitea_url=endpoint, api_token=token)
        with patch('routes.ai_review_routes.server', return_value=target):
            result = web.delete('/api/ai-review/repositories/' + str(saved['id']),
                                json={'revision': saved['revision']}, headers={'X-CSRF-Token': csrf})
        assert result.status_code == 200, result.json
        assert result.json['removal_complete'] and not result.json['enabled']
        hooks = api('GET', prefix + '/hooks')
        assert [item['id'] for item in hooks] == [other['id']]
        assert api('GET', prefix)['id'] == repo['id']
        assert state.job(job)['repository'] == saved['id'] and state.job(job)['status'] == 'superseded'
        fresh = state.save_repo(1, repo['id'], config)
        assert fresh['id'] != saved['id']

        # 空仓库无需先提交代码；真实通知创建后验证失败也可以安全移除。
        empty = api('POST', '/user/repos', {'name': 'empty-removal-test', 'private': True, 'auto_init': False})
        empty_prefix = '/repos/' + empty['full_name']
        empty_saved = state.save_repo(1, empty['id'], dict(config, enabled=False, repo_name=empty['full_name']))
        unrelated = api('POST', empty_prefix + '/hooks', {'type': 'gitea', 'active': True,
                        'events': ['pull_request'], 'config': {'url': 'http://another-service.invalid/empty',
                                                             'content_type': 'json'}})
        with patch.dict(os.environ, AI_REVIEW_WEBHOOK_HOSTS='manager.invalid'), \
                patch('routes.ai_review_routes.server', return_value=target):
            failed = web.post('/api/ai-review/repositories/' + str(empty_saved['id']) + '/webhook',
                              json={'manager_url': 'http://manager.invalid'}, headers={'X-CSRF-Token': csrf})
            assert failed.status_code == 400 and '空仓库' in failed.json['error'], failed.json
            assert state.repo(empty_saved['id'])['webhook_binding']['phase'] == 'verifying'
            assert len(api('GET', empty_prefix + '/hooks')) == 2
            removed = web.delete('/api/ai-review/repositories/' + str(empty_saved['id']),
                                 json={'revision': empty_saved['revision']}, headers={'X-CSRF-Token': csrf})
        assert removed.status_code == 200 and removed.json['removal_complete'], removed.json
        assert [item['id'] for item in api('GET', empty_prefix + '/hooks')] == [unrelated['id']]
        assert api('GET', empty_prefix)['empty'] is True
        print(json.dumps({'说明': '独立官方 Gitea 1.26.4 与实际 Flask 删除接口验收；无模型调用、无用户现有环境操作',
                          '结果': ['只删除本接入 Webhook，其他服务通知保留', '实际 Gitea 仓库保留',
                                   '排队任务停止且历史保留', '相同仓库重新接入使用新记录，旧任务不迁移',
                                   '空仓库接入验证失败后可移除，不需首个提交且其他通知保留']}, ensure_ascii=False))


def main():
    name = 'gm-review-removal-' + uuid.uuid4().hex[:10]
    image = os.environ.get('REVIEW_TEST_IMAGE', 'gitea-review-backend:master-20261027-r1')
    def run(args, **kwargs):
        result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        if result.returncode:
            raise RuntimeError('隔离验收命令失败：' + result.stderr[-3000:])
        return result.stdout
    created = False
    try:
        run(['docker', 'network', 'create', '--label', 'io.gitea-manager.review-removal-test=true', name]); created = True
        run(['docker', 'run', '-d', '--rm', '--platform', 'linux/amd64', '--name', name, '--network', name,
             '--network-alias', 'gitea', '--label', 'io.gitea-manager.review-removal-test=true',
             '-e', 'GITEA__database__DB_TYPE=sqlite3', '-e', 'GITEA__security__INSTALL_LOCK=true',
             '-e', 'GITEA__webhook__ALLOWED_HOST_LIST=*', 'gitea/gitea:1.26.4'])
        import time
        for _ in range(60):
            probe = subprocess.run(['docker', 'exec', name, 'wget', '-q', '-O', '/dev/null', 'http://127.0.0.1:3000/api/v1/version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if probe.returncode == 0: break
            time.sleep(1)
        else: raise RuntimeError('独立 Gitea 启动超时')
        credentials = {'username': 'removal-admin', 'password': secrets.token_urlsafe(30)}
        run(['docker', 'exec', '--user', 'git', name, 'gitea', 'admin', 'user', 'create',
             '--config', '/data/gitea/conf/app.ini', '--username', credentials['username'],
             '--password', credentials['password'], '--email', 'removal@example.invalid', '--admin', '--must-change-password=false'])
        source = [] if '--packaged' in sys.argv else ['-v', str(ROOT / 'backend') + ':/app:ro']
        result = run(['docker', 'run', '--rm', '-i', '--platform', 'linux/amd64', '--network', name,
                      '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONPATH=/app', *source,
                      '-v', str(Path(__file__).resolve()) + ':/acceptance.py:ro', '-w', '/app',
                      image, 'python', '/acceptance.py', '--inside'], input=json.dumps(credentials))
        destination = ROOT / 'work/review-repositories'; destination.mkdir(parents=True, exist_ok=True)
        (destination / 'live-acceptance.json').write_text(result)
        print(result)
    finally:
        subprocess.run(['docker', 'rm', '-fv', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if created: subprocess.run(['docker', 'network', 'rm', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    inside() if '--inside' in sys.argv else main()
