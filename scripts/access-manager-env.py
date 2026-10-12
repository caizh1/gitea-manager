#!/usr/bin/env python3
"""在真实目录的独立网络中启动双进程 Manager 与 HTTPS 浏览器入口。"""
import argparse
import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('access_env', ROOT / 'scripts/access-env.py')
env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(env)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',nargs='?',choices=['up','status','down'],default='up')
    parser.add_argument('--packaged',action='store_true',help='使用固定交付镜像，不挂载应用源码或前端构建目录')
    parser.add_argument('--image-tag',help='同时指定前后端交付镜像标签')
    options=parser.parse_args()
    values = env.values()
    state = env.STATE / 'manager'
    state.mkdir(exist_ok=True)
    config_path = state / 'runtime.json'
    if config_path.exists():
        config = json.loads(config_path.read_text())
    else:
        config = {'port': env.reserve_ports(1)[0], 'password': 'Aa1!' + secrets.token_urlsafe(24),
                  'session_key': secrets.token_urlsafe(48), 'authorization_token': secrets.token_urlsafe(48)}
        env.write_secrets(config_path, json.dumps(config))
    if 'http_port' not in config:
        config['http_port'] = env.reserve_ports(1)[0]
        env.write_secrets(config_path, json.dumps(config))
    project = values['ACCESS_PROJECT']
    backend = project + '-manager-backend'
    frontend = project + '-manager-frontend'
    if options.action == 'status':
        print(json.dumps({'入口': 'https://localhost:' + str(config['port']), '本地应急管理员入口': 'http://localhost:' + str(config['http_port']), '后端': backend, '前端': frontend}, ensure_ascii=False))
        return
    if options.action == 'down':
        for name in (backend, frontend):
            found = subprocess.run(['docker', 'inspect', name], capture_output=True)
            if found.returncode == 0:
                label = json.loads(found.stdout)[0]['Config']['Labels'].get('gitea-manager.access-test')
                if label != project:
                    raise RuntimeError('容器标记不匹配，拒绝操作')
                env.run(['docker', 'rm', '-f', name])
        print('仅移除本验收项目的 Manager 容器；数据库原生卷和证据保留。')
        return
    env.write_secrets(state / 'authorization.token', config['authorization_token'])
    env.write_secrets(state / 'runtime.env', '\n'.join([
        'SECRET_KEY=' + config['session_key'], 'INIT_PASSWORD=' + config['password'],
        'DATABASE_URL=sqlite:////access-data/gitea-manager.db', 'AI_REVIEW_DB=/access-data/review.db',
        'AI_REVIEW_MASTER_KEY_FILE=/access-data/review.key', 'AI_REVIEW_AUTHORIZATION_TOKEN_FILE=/run/secrets/access_authorization',
        'AI_REVIEW_AUTHORIZATION_URL=http://' + backend + ':5000/api/internal/authorization/check',
        'TRUSTED_PROXY_HOSTS=' + frontend, 'AI_REVIEW_GITEA_HOSTS=gitea,enhanced',
    ]) + '\n')
    (state / 'nginx.conf').write_text('''server {
    listen 443 ssl;
    ssl_certificate /tls/trusted.pem;
    ssl_certificate_key /tls/trusted.key;
    root /usr/share/nginx/html;
    location / { try_files $uri $uri/ /index.html; }
    location /api/ {
        proxy_pass http://''' + backend + ''':5000;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 120s;
    }
}
''')
    with (state / 'nginx.conf').open('a') as handle:
        handle.write('''server {
    listen 80;
    root /usr/share/nginx/html;
    location / { try_files $uri $uri/ /index.html; }
    location /api/ {
        proxy_pass http://''' + backend + ''':5000;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 120s;
    }
}
''')
    network = project + '_default'
    for name in (backend, frontend):
        found = subprocess.run(['docker', 'inspect', name], capture_output=True)
        if found.returncode == 0:
            label = json.loads(found.stdout)[0]['Config']['Labels'].get('gitea-manager.access-test')
            if label != project:
                raise RuntimeError('容器标记不匹配，拒绝操作')
            env.run(['docker', 'rm', '-f', name])
    data = project + '-manager-data'
    env.run(['docker', 'volume', 'create', '--label', 'gitea-manager.access-test=' + project, data])
    backend_image='gitea-review-backend:'+(options.image_tag or ('access-20261011-r2' if options.packaged else 'access-20261011-r1'))
    frontend_image='gitea-review-frontend:'+(options.image_tag or ('access-20261011-r2' if options.packaged else 'manager-standard-20261010-r1'))
    backend_mounts=[] if options.packaged else ['-v',str(ROOT/'backend')+':/app:ro']
    frontend_mounts=[] if options.packaged else ['-v',str(ROOT/'frontend/dist')+':/usr/share/nginx/html:ro']
    env.run(['docker', 'run', '-d', '--platform', 'linux/amd64', '--name', backend, '--network', network,
             '--label', 'gitea-manager.access-test=' + project, '--env-file', str(state / 'runtime.env'),
             *backend_mounts, '-v', data + ':/access-data', '-v', '/var/run/docker.sock:/var/run/docker.sock',
             '-v', str(state / 'authorization.token') + ':/run/secrets/access_authorization:ro',
             backend_image, 'gunicorn', '-w', '2', '-b', '0.0.0.0:5000',
             '--timeout', '120', 'app:create_app()'])
    env.run(['docker', 'exec', backend, 'python', '-c', "from pathlib import Path;from cryptography.fernet import Fernet;p=Path('/access-data/review.key');p.exists() or p.write_bytes(Fernet.generate_key());p.chmod(0o600)"])
    env.run(['docker', 'run', '-d', '--platform', 'linux/amd64', '--name', frontend, '--network', network,
             '--label', 'gitea-manager.access-test=' + project, '-p', '127.0.0.1:' + str(config['port']) + ':443',
             '-p', '127.0.0.1:' + str(config['http_port']) + ':80',
             *frontend_mounts,
             '-v', str(state / 'nginx.conf') + ':/etc/nginx/conf.d/default.conf:ro',
             '-v', str(env.STATE / 'fixtures') + ':/tls:ro', frontend_image])
    evidence=[]
    for name in (backend,frontend):
        detail=json.loads(env.run(['docker','inspect',name]))[0]
        destinations=[m['Destination'] for m in detail['Mounts']]
        if options.packaged and any(path in destinations for path in ('/app','/usr/share/nginx/html')):
            raise RuntimeError('镜像验收环境出现源码挂载，拒绝记录通过')
        evidence.append({'容器':name,'镜像摘要':detail['Image'],'挂载目标':destinations,'使用交付镜像':options.packaged})
    (state/'runtime-images.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'入口': 'https://localhost:' + str(config['port']), '本地应急管理员入口': 'http://localhost:' + str(config['http_port']), '状态': '双进程与HTTPS入口已启动'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
