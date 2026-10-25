"""在已有 Docker 的离线主机创建独立实例并执行同一条恢复验证链。"""
import argparse
import configparser
import io
import json
import os
from pathlib import Path
import secrets
import time
import zipfile

import docker
from flask import Flask
import requests


def main():
    bundle = Path(os.environ.get('DISASTER_BUNDLE_DIR', '/bundle'))
    parser = argparse.ArgumentParser(description='离线灾备：创建独立实例，不覆盖既有容器')
    parser.add_argument('--http-port', type=int, default=3000)
    parser.add_argument('--ssh-port', type=int, default=2222)
    parser.add_argument('--url', default=None)
    args = parser.parse_args()
    args.url = args.url or f'http://localhost:{args.http_port}/'
    from urllib.parse import urlsplit
    public_url = urlsplit(args.url)
    if public_url.scheme not in ('http', 'https') or not public_url.hostname:
        raise RuntimeError('对外地址必须是完整 HTTP 或 HTTPS 地址')
    if not all(0 < port < 65536 for port in (args.http_port, args.ssh_port)):
        raise RuntimeError('端口必须在 1 至 65535 范围内')
    from models import db, GiteaServer, Backup, RestoreTask
    import access.models
    import access.scopes
    from services.backup_manifest import load_manifest
    from services.restore_secrets import put_file, SECRET_URI
    from services.gitea_service import do_restore
    from services.restore_recovery import recover, STATE
    from routes.backup_routes import import_backup
    state_path = Path('/app/data/disaster-state.json')
    import fcntl
    state_path.parent.mkdir(parents=True, exist_ok=True)
    manager_lock = (state_path.parent / 'disaster-bootstrap.lock').open('a')
    fcntl.flock(manager_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:////app/data/disaster-manager.db', LOGIN_DISABLED=True)
    db.init_app(app)
    client = docker.from_env()
    with app.app_context():
        db.create_all()
        metadata = json.loads((bundle / 'bundle.json').read_text())
        manifest = load_manifest(str(bundle / 'backup.zip'))
        if manifest is None or metadata['镜像']['gitea'] != manifest['准入']['镜像']:
            raise RuntimeError('离线包与备份构建不一致')
        if state_path.exists():
            pending = json.loads(state_path.read_text())
            if pending.get('准备'):
                import re
                from docker.errors import NotFound
                marker = pending['准备']
                if not re.fullmatch(r'gitea-disaster-[a-f0-9]{12}', marker):
                    raise RuntimeError('初始化恢复记录无效')
                for container in client.containers.list(all=True, filters={'label': 'gitea.manager.disaster=' + marker}):
                    container.remove(force=True)
                for suffix in ('-data', '-postgres'):
                    try:
                        client.volumes.get(marker + suffix).remove()
                    except NotFound:
                        pass
                for network in client.networks.list(filters={'label': 'gitea.manager.disaster=' + marker}):
                    network.reload()
                    for identifier in network.attrs.get('Containers') or {}:
                        network.disconnect(identifier, force=True)
                    network.remove()
                state_path.unlink()
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if state.get('备份摘要') != hashlib_file(str(bundle / 'backup.zip')):
                raise RuntimeError('此恢复卷属于其他备份；请保留原卷并使用另一独立恢复环境')
            target = db.session.get(GiteaServer, state['目标'])
            container = client.containers.get(target.gitea_container)
            client.networks.get(state['网络']).connect(client.containers.get(os.environ['HOSTNAME']))
            from services.restore_secrets import read_file
            from docker.errors import NotFound
            try:
                read_file(container, STATE)
            except NotFound:
                if state.get('完成'):
                    print('此备份已恢复，未覆盖当前数据。', flush=True)
                    return
            else:
                print(recover(target), flush=True)
                task = db.session.get(RestoreTask, state.get('任务'))
                if task and task.status == 'success':
                    state['完成'] = True
                    write_state(state_path, state)
                    return
            backup = db.session.get(Backup, state['备份'])
        else:
            for name in ('postgres', 'gitea'):
                with open(str(bundle / (name + '.tar')), 'rb') as stream:
                    for _ in client.api.load_image(stream):
                        pass
                client.images.get(metadata['镜像'][name])
            marker = 'gitea-disaster-' + secrets.token_hex(6)
            write_state(state_path, {'准备': marker})
            from services.restore_isolation import available_ipam
            network = client.networks.create(marker, ipam=available_ipam(client), labels={'gitea.manager.disaster': marker})
            network.connect(client.containers.get(os.environ['HOSTNAME']))
            password = secrets.token_hex(32)
            pg = client.containers.run(metadata['镜像']['postgres'], name=marker + '-db', network=network.name,
                environment={'POSTGRES_USER': 'gitea', 'POSTGRES_DB': 'gitea', 'POSTGRES_PASSWORD': password},
                volumes={marker + '-postgres': {'bind': '/var/lib/postgresql/data', 'mode': 'rw'}},
                labels={'gitea.manager.disaster': marker}, detach=True, restart_policy={'Name': 'unless-stopped'})
            deadline = time.monotonic() + 120
            while pg.exec_run(['pg_isready', '-U', 'gitea']).exit_code:
                if time.monotonic() > deadline:
                    raise RuntimeError('离线 PostgreSQL 未就绪；现场已保留')
                time.sleep(1)
            with zipfile.ZipFile(str(bundle / 'backup.zip')) as archive:
                from services.restore_archive import validate_ssh_archive
                host_keys = archive.read('manager-ssh-hostkeys.tar')
                validate_ssh_archive(host_keys)
                names = archive.namelist()
                sql = next(name for name in names if name.endswith('gitea-db.sql'))
                root = str(Path(sql).parent)
                ini_name = 'app.ini' if root == '.' else root + '/app.ini'
                config = configparser.ConfigParser(interpolation=None)
                config.optionxform = str
                config.read_string('[DEFAULT]\n' + archive.read(ini_name).decode())
            overrides = {'database': {'DB_TYPE': 'postgres', 'HOST': pg.name + ':5432', 'NAME': 'gitea', 'USER': 'gitea', 'PASSWD': password},
                'server': {'APP_DATA_PATH': '/data/gitea', 'HTTP_ADDR': '0.0.0.0', 'HTTP_PORT': '3000', 'PROTOCOL': 'http',
                           'ROOT_URL': args.url, 'SSH_PORT': str(args.ssh_port), 'DOMAIN': public_url.hostname, 'SSH_DOMAIN': public_url.hostname},
                'repository': {'ROOT': '/data/git/repositories'},
                'security': {'INSTALL_LOCK': 'true', 'SECRET_KEY': '', 'SECRET_KEY_URI': SECRET_URI}}
            for section, values in overrides.items():
                if not config.has_section(section):
                    config.add_section(section)
                for key, value in values.items():
                    config.set(section, key, value)
            config.set('server', 'HTTP_ADDR', '127.0.0.1')
            container = client.containers.create(metadata['镜像']['gitea'], name=marker + '-gitea', network=network.name,
                volumes={marker + '-data': {'bind': '/data', 'mode': 'rw'}},
                ports={'3000/tcp': args.http_port, '22/tcp': args.ssh_port},
                labels={'gitea.manager.disaster': marker}, restart_policy={'Name': 'unless-stopped'})
            client.containers.run(metadata['镜像']['gitea'], entrypoint=['sh', '-ec'],
                command=['mkdir -p /data/gitea/conf; chown -R 1000:1000 /data/gitea'],
                volumes_from=[container.id], network_mode='none', remove=True)
            from services.backup_dependencies import install_startup_files
            with zipfile.ZipFile(str(bundle / 'backup.zip')) as archive:
                install_startup_files(archive, config, container)
            from services.backup_actions import external_path
            raw = io.StringIO()
            config.write(raw)
            artifacts = external_path(raw.getvalue().removeprefix('[DEFAULT]\n').encode())
            if artifacts:
                client.containers.run(metadata['镜像']['gitea'], entrypoint=['sh', '-ec'],
                    command=['mkdir -p "$1"; chown 1000:1000 "$1"', 'sh', artifacts],
                    volumes_from=[container.id], network_mode='none', remove=True)
            if not container.put_archive('/data', host_keys):
                raise RuntimeError('SSH 主机身份写入失败')
            put_file(container, '/data/gitea/conf/app.ini', raw.getvalue().removeprefix('[DEFAULT]\n').encode(), 0o600, 1000, 1000)
            put_file(container, '/data/gitea/conf/manager-secret-key', secrets.token_hex(32).encode(), 0o600, 1000, 1000)
            container.start()
            url = 'http://' + container.name + ':3000'
            deadline = time.monotonic() + 120
            while True:
                try:
                    if container.exec_run(['wget', '-q', '-T', '2', '-O', '-', 'http://127.0.0.1:3000/api/v1/version']).exit_code == 0:
                        break
                except requests.RequestException:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError('离线 Gitea 未就绪；现场已保留')
                time.sleep(1)
            container.stop(timeout=30)
            config.set('server', 'HTTP_ADDR', '0.0.0.0')
            raw = io.StringIO()
            config.write(raw)
            put_file(container, '/data/gitea/conf/app.ini', raw.getvalue().removeprefix('[DEFAULT]\n').encode(), 0o600, 1000, 1000)
            target = GiteaServer(name='离线灾备恢复实例', host='127.0.0.1', ssh_user='root', gitea_port=args.http_port,
                gitea_url=url, api_token='', is_local=True, connection_mode='local', gitea_container=container.name,
                pg_container=pg.name, pg_dbname='gitea', pg_user='gitea')
            db.session.add(target)
            db.session.commit()
            with open(str(bundle / 'backup.zip'), 'rb') as stream, app.test_request_context('/api/backups/import', method='POST',
                    data={'file': (stream, 'backup.zip')}):
                response, status = import_backup()
                if status != 201:
                    raise RuntimeError('离线备份导入失败')
                backup = db.session.get(Backup, response.json['id'])
            state = {'目标': target.id, '备份': backup.id, '网络': network.name, '备份摘要': hashlib_file(str(bundle / 'backup.zip'))}
            write_state(state_path, state)
            state_path.chmod(0o600)
        task = RestoreTask(backup_id=backup.id, target_server_id=target.id, target_server_name=target.name, status='running')
        db.session.add(task)
        db.session.commit()
        state['任务'] = task.id
        write_state(state_path, state)
        do_restore(task.id)
        if task.status != 'success':
            raise RuntimeError('灾备未完成，现场与检查点已保留：' + task.error_msg)
        state['完成'] = True
        write_state(state_path, state)
        print('离线灾备恢复完成，入口：' + args.url, flush=True)


def write_state(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def hashlib_file(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


if __name__ == '__main__':
    main()
