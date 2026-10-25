"""隔离的 LDAP、OAuth、SMTP 和 Webhook 接收器；绝不转发真实消息。"""
import asyncore
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import resource
import socket
import smtpd
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

ROOT = Path('/state')
CONFIG = json.loads((ROOT / 'integration-private.json').read_text())


def record(kind, **fields):
    with (ROOT / 'integration-events.jsonl').open('a') as stream:
        stream.write(json.dumps({'类型': kind, '时间': time.time(), **fields}, ensure_ascii=False) + '\n')


class Mail(smtpd.SMTPServer):
    def process_message(self, peer, mailfrom, rcpttos, data, **kwargs):
        record('邮件', 发件人=mailfrom, 收件人=rcpttos, 字节=len(data), SHA256=hashlib.sha256(data).hexdigest())


class HTTP(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        request = urlparse(self.path)
        if request.path == '/authorize':
            query = parse_qs(request.query)
            callback = query.get('redirect_uri', [''])[0]
            allowed = CONFIG['callbacks']
            if callback not in allowed or query.get('client_id') != ['remote-lab']:
                return self.reply({'错误': 'OAuth 回调或客户端不在白名单'}, 400)
            self.send_response(302)
            self.send_header('Location', callback + '?' + urlencode({'code': 'lab-once', 'state': query['state'][0]}))
            self.end_headers()
            record('OAuth授权', 回调=callback)
        elif request.path == '/user':
            if self.headers.get('Authorization') not in ('Bearer lab-access', 'token lab-access'):
                return self.reply({'错误': '无效令牌'}, 401)
            self.reply({'id': 9001, 'login': 'laboauth', 'name': '隔离 OAuth 用户', 'email': 'oauth@lab.invalid', 'avatar_url': ''})
            record('OAuth身份')
        elif request.path == '/emails':
            self.reply([{'email': 'oauth@lab.invalid', 'primary': True, 'verified': True}])
        elif request.path == '/health':
            self.reply({'状态': '就绪'})
        else:
            self.reply({'错误': '不存在'}, 404)

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        path = urlparse(self.path).path
        if path == '/token':
            fields = parse_qs(data.decode())
            valid = fields.get('client_secret') == [CONFIG['oauth_secret']]
            if not valid:
                import base64
                valid = self.headers.get('Authorization') == 'Basic ' + base64.b64encode(('remote-lab:' + CONFIG['oauth_secret']).encode()).decode()
            if not valid or fields.get('code') != ['lab-once']:
                return self.reply({'错误': 'OAuth 凭据无效'}, 401)
            self.reply({'access_token': 'lab-access', 'token_type': 'Bearer', 'scope': 'user:email'})
            record('OAuth换令牌')
        elif path.startswith('/webhook/'):
            digest = hmac.new(CONFIG['webhook_secret'].encode(), data, hashlib.sha256).hexdigest()
            valid = hmac.compare_digest(self.headers.get('X-Gitea-Signature', ''), digest)
            record('Webhook', 签名有效=valid, 事件=self.headers.get('X-Gitea-Event'), 路径=path, SHA256=hashlib.sha256(data).hexdigest())
            self.reply({'签名有效': valid}, 200 if valid else 403)
        else:
            self.reply({'错误': '不存在'}, 404)


def ldap():
    folder = ROOT / 'ldap'
    folder.mkdir(exist_ok=True)
    config = folder / 'slapd.conf'
    config.write_text('''include /etc/ldap/schema/core.schema
include /etc/ldap/schema/cosine.schema
include /etc/ldap/schema/inetorgperson.schema
pidfile /state/ldap/slapd.pid
argsfile /state/ldap/slapd.args
modulepath /usr/lib/ldap
moduleload back_mdb
database mdb
maxsize 10485760
suffix "dc=lab,dc=invalid"
rootdn "cn=admin,dc=lab,dc=invalid"
rootpw ''' + CONFIG['ldap_password'] + '''
directory /state/ldap
access to attrs=userPassword by anonymous auth by self write by * none
access to * by * read
''')
    config.chmod(0o600)
    if not (folder / 'data.mdb').exists():
        body = '''dn: dc=lab,dc=invalid
objectClass: domain
dc: lab

dn: uid=labldap,dc=lab,dc=invalid
objectClass: inetOrgPerson
cn: 隔离目录用户
sn: 测试
uid: labldap
mail: ldap@lab.invalid
userPassword: ''' + CONFIG['ldap_password'] + '\n'
        subprocess.run(['slapadd', '-f', str(config)], input=body.encode(), check=True)
    log = folder / 'slapd.log'
    log.touch(mode=0o600, exist_ok=True)
    with log.open('ab') as stream:
        process = subprocess.Popen(['slapd', '-f', str(config), '-h', 'ldap://0.0.0.0:1389', '-d', '256'],
                                   stdout=stream, stderr=stream)
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError('隔离 LDAP 启动失败，原始诊断保存在受限的 ldap/slapd.log')
        try:
            with socket.create_connection(('127.0.0.1', 1389), timeout=0.2):
                return process
        except OSError:
            time.sleep(0.1)
    process.terminate()
    raise RuntimeError('隔离 LDAP 未在启动期限内监听')


if __name__ == '__main__':
    # slapd 按文件描述符上限分配连接表，Docker 默认高上限会让小夹具触发 OOM。
    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
    process = ldap()
    Mail(('0.0.0.0', 1025), None, decode_data=False)
    threading.Thread(target=asyncore.loop, daemon=True).start()
    try:
        ThreadingHTTPServer(('0.0.0.0', 8081), HTTP).serve_forever()
    finally:
        process.terminate()
