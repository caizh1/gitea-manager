"""为真实组织角色验收提供独立 TLS 入口，不修改被测容器或导出宿主端口。"""
import json
import shutil
import time


def configure(root, enabled):
    from .host import LABEL, assert_owned, output, run, service_id, write_json
    active = root / 'manager-role-tls-active.json'
    if not enabled:
        if not active.exists():
            return {'说明': '没有本轮活动的角色 TLS 入口'}
        record = json.loads(active.read_text())
        attrs = assert_owned(root, record['容器'])
        if (attrs['Image'] != record['镜像'] or
                attrs['HostConfig']['NetworkMode'] != 'container:' + record['后端容器']):
            raise RuntimeError('角色 TLS 入口身份变化，拒绝停止未知容器')
        if attrs['State']['Running']:
            run(['docker', 'stop', record['容器']], capture=True)
        record['已停止并保留'] = True
        write_json(root / record['记录文件'], record)
        active.unlink()
        return {'说明': '本轮 TLS 入口已停止，容器及证书现场保留'}
    if shutil.disk_usage(root).free < 6 * 1024**3:
        raise RuntimeError('角色验收只保留元数据，仍要求六 GiB 余量')
    if active.exists():
        raise RuntimeError('已有待接管的角色 TLS 入口，拒绝覆盖记录')
    backend = service_id(root, 'backend')
    attrs = assert_owned(root, backend)
    images = json.loads((root / 'images.json').read_text())
    if attrs['Image'] != images['manager']['摘要']:
        raise RuntimeError('实际后端不是冻结镜像，未创建角色入口')
    stamp = str(time.time_ns())
    folder = root / ('manager-role-tls-' + stamp)
    folder.mkdir(mode=0o700)
    # 独立 CA 与服务证书；客户端必须校验证书及 backend 主机名。
    run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
         '-keyout', str(folder / 'ca.key'), '-out', str(folder / 'ca.crt'),
         '-subj', '/CN=RemoteBackupRoleCA', '-addext', 'basicConstraints=critical,CA:TRUE'], capture=True)
    run(['openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes',
         '-keyout', str(folder / 'server.key'), '-out', str(folder / 'server.csr'),
         '-subj', '/CN=backend'], capture=True)
    (folder / 'extensions.conf').write_text('subjectAltName=DNS:backend\nextendedKeyUsage=serverAuth\n')
    run(['openssl', 'x509', '-req', '-in', str(folder / 'server.csr'),
         '-CA', str(folder / 'ca.crt'), '-CAkey', str(folder / 'ca.key'), '-CAcreateserial',
         '-out', str(folder / 'server.crt'), '-days', '2', '-extfile', str(folder / 'extensions.conf')], capture=True)
    (folder / 'nginx.conf').write_text('''events {}
http {
  access_log off;
  server {
    listen 5443 ssl;
    ssl_certificate /role/server.crt;
    ssl_certificate_key /role/server.key;
    location / {
      proxy_pass http://127.0.0.1:5000;
      proxy_set_header Host $http_host;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_set_header X-Forwarded-For $remote_addr;
    }
  }
}
''')
    for path in folder.iterdir():
        path.chmod(0o600)
    identifier = output(['docker', 'create', '--name', 'gm-role-tls-' + stamp,
        '--label', LABEL + '=' + root.name, '--label', 'gitea.manager.test-role=tls',
        '--network', 'container:' + backend, '-v', str(folder) + ':/role:ro',
        '--entrypoint', 'nginx', images['frontend']['摘要'], '-c', '/role/nginx.conf', '-g', 'daemon off;'])
    record = {'说明': '只代理实际 TLS 请求到同网络命名空间的回环后端，无宿主端口',
              '容器': identifier, '后端容器': backend, '镜像': images['frontend']['摘要'],
              'URL': 'https://backend:5443/api', 'CA文件': '/evidence/' + folder.name + '/ca.crt',
              '记录文件': 'manager-role-tls-' + stamp + '.json'}
    write_json(active, record)
    write_json(root / record['记录文件'], record)
    run(['docker', 'start', identifier], capture=True)
    return record
