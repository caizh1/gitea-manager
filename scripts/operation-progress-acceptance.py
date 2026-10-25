#!/usr/bin/env python3
"""仅创建带本次标记的隔离容器，验证实际备份恢复及只读页面。"""
import base64
import io
import json
import os
from pathlib import Path
import secrets
import sys
import tarfile
import tempfile
import threading
import time
import zipfile

import docker
import requests
from flask import Flask, send_from_directory

root = Path(os.environ.get('ACCEPTANCE_ROOT') or tempfile.mkdtemp(prefix='operation-acceptance-'))
os.environ['BACKUP_DIR'] = str(root / 'backups')
os.environ['MANAGEMENT_DIAGNOSTICS_ROOT'] = str(root / 'diagnostics')
os.environ['SSH_KEY_PATH'] = str(root / 'ssh-key')
os.environ['SSH_KNOWN_HOSTS'] = str(root / 'known-hosts')
sys.path.insert(0, '/app')
from models import db, Backup, RestoreTask, GiteaServer, BackupStepLog, RestoreStepLog
from services.gitea_service import do_backup, do_restore

client = docker.from_env()
marker = 'operation-acceptance-' + secrets.token_hex(4)
owned = []
records = []
app = Flask(__name__, static_folder='/ui/assets', static_url_path='/assets')
app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///' + str(root / 'manager.db'), LOGIN_DISABLED=True, SECRET_KEY=secrets.token_hex(32))
db.init_app(app)
from auth import init_auth
init_auth(app)
from routes.backup_routes import backup_bp
from routes.restore_routes import restore_bp
from routes.server_routes import server_bp
from routes.dashboard_routes import dashboard_bp
app.register_blueprint(backup_bp, url_prefix='/api')
app.register_blueprint(restore_bp, url_prefix='/api')
app.register_blueprint(server_bp, url_prefix='/api')
app.register_blueprint(dashboard_bp, url_prefix='/api')
@app.before_request
def readonly():
    from flask import request
    if request.method != 'GET': return {'error': '隔离验收页面仅供读取'}, 405
@app.get('/api/session')
def session(): return {'authenticated': True}
@app.get('/api/settings')
def settings(): return {'host_ip': '127.0.0.1'}
@app.get('/api/alerts/summary')
def alerts(): return {'total': 0}
@app.get('/api/alerts')
def alert_list(): return []
@app.get('/api/ai-review/attention')
def attention(): return {'total': 0, 'items': [], 'tasks': []}
@app.get('/api/schedules')
def schedules(): return []
@app.get('/')
@app.get('/<path:path>')
def ui(path=''): return send_from_directory('/ui', 'index.html')


def note(label, **values):
    value = {'检查': label, **values}
    records.append(value); print(json.dumps(value, ensure_ascii=False), flush=True)
    (Path('/evidence') / os.environ.get('ACCEPTANCE_RESULTS', 'operation-progress-real.json')).write_text(json.dumps({'说明': '隔离真实容器验收的逐项记录', '检查': records}, ensure_ascii=False, indent=2)+'\n')


def upload(container, dest, name, content, mode=0o600):
    content = content.encode() if isinstance(content, str) else content
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        item = tarfile.TarInfo(name); item.size = len(content); item.mode = mode
        item.uid = item.gid = 1000 if dest == '/data' else 0
        archive.addfile(item, io.BytesIO(content))
    assert container.put_archive(dest, stream.getvalue())


def create(image, suffix, **kwargs):
    if os.environ.get('ACCEPTANCE_NETWORK') and 'network_mode' not in kwargs:
        kwargs.setdefault('network', os.environ['ACCEPTANCE_NETWORK'])
    value = client.containers.create(image, name=marker+'-'+suffix, labels={'manager.progress.acceptance': marker}, platform='linux/amd64', **kwargs)
    owned.append(value)
    return value


def node(suffix):
    password = secrets.token_hex(16)
    pg = create('postgres:17.11-bookworm', suffix+'-db', environment={'POSTGRES_USER': 'gitea', 'POSTGRES_DB': 'gitea', 'POSTGRES_PASSWORD': password})
    pg.start(); pg.reload(); host = pg.attrs['NetworkSettings']['Networks'][os.environ.get('ACCEPTANCE_NETWORK', 'bridge')]['IPAddress']
    deadline = time.monotonic() + 90
    while pg.exec_run(['pg_isready', '-U', 'gitea']).exit_code:
        if time.monotonic() > deadline: raise RuntimeError('隔离数据库未就绪')
        time.sleep(1)
    env = {'GITEA__database__DB_TYPE': 'postgres', 'GITEA__database__HOST': host+':5432', 'GITEA__database__NAME': 'gitea', 'GITEA__database__USER': 'gitea', 'GITEA__database__PASSWD': password,
           'GITEA__security__INSTALL_LOCK': 'true', 'GITEA__security__SECRET_KEY_URI': 'file:/data/gitea/conf/manager-secret-key', 'GITEA__service__DISABLE_REGISTRATION': 'true', 'GITEA__repository__DEFAULT_BRANCH': 'main'}
    gitea = create('gitea/gitea:1.26.4', suffix, environment=env)
    upload(gitea, '/data', 'gitea/conf/manager-secret-key', secrets.token_hex(32))
    gitea.start(); gitea.reload(); url = 'http://' + gitea.attrs['NetworkSettings']['Networks'][os.environ.get('ACCEPTANCE_NETWORK', 'bridge')]['IPAddress'] + ':3000'
    deadline = time.monotonic() + 120
    while True:
        try:
            if requests.get(url+'/api/v1/version', timeout=2).status_code == 200: break
        except requests.RequestException: pass
        if time.monotonic() > deadline: raise RuntimeError('隔离 Gitea 未就绪')
        time.sleep(1)
    result = gitea.exec_run(['/usr/local/bin/gitea', '-c', '/data/gitea/conf/app.ini', 'admin', 'user', 'create', '--username', 'progressadmin', '--password', password, '--email', 'progress@example.test', '--admin', '--must-change-password=false'], user='git')
    if result.exit_code: raise RuntimeError('隔离用户创建失败')
    response = requests.post(url+'/api/v1/users/progressadmin/tokens', auth=('progressadmin', password), json={'name': 'progress', 'scopes': ['all']}, timeout=10)
    response.raise_for_status(); token = response.json()['sha1']
    server = GiteaServer(name='隔离'+suffix, host='127.0.0.1', ssh_user='root', gitea_port=3000, gitea_url=url, api_token=token,
                         is_local=True, connection_mode='local', gitea_container=gitea.name, pg_container=pg.name, pg_dbname='gitea', pg_user='gitea', role='primary' if suffix == 'source' else 'backup')
    db.session.add(server); db.session.commit()
    return server, gitea, password


def backup(server, filename):
    task = Backup(source_server_id=server.id, source_server_name=server.name, source_api_token=server.api_token, filename=filename, file_path='', status='running', commit_snapshot_status='pending')
    db.session.add(task); db.session.commit(); identifier = task.id
    do_backup(identifier); db.session.expire_all(); task = db.session.get(Backup, identifier)
    if task.status != 'success': raise RuntimeError('真实备份失败：' + task.error_msg)
    assert zipfile.is_zipfile(task.file_path)
    steps = BackupStepLog.query.filter_by(backup_id=identifier).all()
    assert all(s.status == 'success' for s in steps)
    note('真实备份通过', 模式=server.connection_mode, 仓库数=task.commit_snapshot_repo_count, 步骤数=len(steps), 任务=identifier)
    return task


def restore(backup, target, expected='success'):
    task = RestoreTask(backup_id=backup.id, target_server_id=target.id, target_server_name=target.name, status='running')
    db.session.add(task); db.session.commit(); identifier = task.id
    do_restore(identifier); db.session.expire_all(); task = db.session.get(RestoreTask, identifier)
    if task.status != expected: raise RuntimeError('真实恢复终态不符：' + task.error_msg)
    from services.commit_service import _require_target_health
    _require_target_health(target)
    assert RestoreStepLog.query.filter_by(restore_task_id=identifier, status='running').count() == 0
    note('真实恢复终态通过', 模式=target.connection_mode, 状态=task.status, 步骤数=RestoreStepLog.query.filter_by(restore_task_id=identifier).count(), 任务=identifier)
    return task


def main():
    try:
        with app.app_context():
            db.create_all()
            from werkzeug.serving import make_server
            web = make_server('0.0.0.0', 5087, app, threaded=True)
            threading.Thread(target=web.serve_forever, daemon=True).start()
            source, source_container, password = node('source')
            target, target_container, _ = node('target')
            note('隔离实例已就绪')
            for index in range(60):
                r = requests.post(source.gitea_url+'/api/v1/user/repos', headers={'Authorization': 'token '+source.api_token}, json={'name': f'sample-{index}', 'auto_init': True, 'private': True}, timeout=30)
                r.raise_for_status()
                if index % 20 == 19: note('准备真实仓库', 数量=index+1)
            sample = backup(source, 'local-sample.zip')
            restore(sample, target)
            git = source_container.exec_run(['git', 'ls-remote', target.gitea_url.replace('http://', 'http://progressadmin:'+password+'@')+'/progressadmin/sample-0.git'])
            if git.exit_code: raise RuntimeError('恢复后的真实 Git 协议访问失败')
            note('恢复后的私有 Git 协议访问通过')
            # 无效 SQL 必须走真实数据库导入与回退，不能只改变状态断言。
            bad_path = root/'invalid-sql.zip'
            with zipfile.ZipFile(sample.file_path) as original, zipfile.ZipFile(bad_path, 'w') as corrupt:
                for member in original.infolist():
                    data = original.read(member.filename)
                    if member.filename.endswith('gitea-db.sql'): data += b'\nTHIS IS NOT VALID SQL;\n'
                    corrupt.writestr(member, data)
            bad = Backup(source_server_id=source.id, filename='invalid-sql.zip', file_path=str(bad_path), status='success', commit_snapshot_status='success')
            db.session.add(bad); db.session.commit()
            restore(bad, target, expected='failed')
            git = source_container.exec_run(['git', 'ls-remote', target.gitea_url.replace('http://', 'http://progressadmin:'+password+'@')+'/progressadmin/sample-0.git'])
            if git.exit_code: raise RuntimeError('回退后的 Git 访问失败')
            note('真实 SQL 失败回退后 Git 可访问')
            note('本轮实际验收完成，保留页面用于只读检查')
            (Path('/evidence') / os.environ.get('ACCEPTANCE_RESULTS', 'operation-progress-real.json')).write_text(json.dumps({'说明': '隔离真实容器验收；同机路径', '检查': records}, ensure_ascii=False, indent=2)+'\n')
                # 页面观察窗口可由环境变量限定，不连接任何用户现有实例。
            time.sleep(int(os.environ.get('PREVIEW_SECONDS', '600')))
    finally:
        for container in reversed(owned):
            try:
                container.reload()
                if container.labels.get('manager.progress.acceptance') == marker: container.remove(force=True, v=True)
            except docker.errors.NotFound: pass
        client.close()


if __name__ == '__main__': main()
