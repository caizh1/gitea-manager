"""管理员接入向导：本机 Docker 直连、远端显式信任及诊断下载。"""
import base64
import configparser
import contextlib
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import time
import uuid
from urllib.parse import urlsplit

import docker
import paramiko
from flask import Blueprint, jsonify, request, send_file, session
from flask_login import login_required

from models import db, GiteaServer, get_setting
from services.diagnostics import Report, CURRENT, register_secret, redact
from services.ssh_service import SSHService

management_bp = Blueprint('management', __name__)


def diagnostic_root():
    return Path(os.environ.get('MANAGEMENT_DIAGNOSTICS', '/app/data/diagnostics'))


@management_bp.before_request
@login_required
def protect():
    origin=request.headers.get('Origin')
    if (origin and urlsplit(origin).netloc!=request.host) or request.headers.get('Sec-Fetch-Site')=='cross-site':
        return jsonify(error='不允许跨站访问管理接口'),403
    if request.content_length and request.content_length>1024*1024:
        return jsonify(error='请求超过大小上限'),413
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        expected=session.get('management_csrf', '')
        from access.control import csrf_valid
        if not csrf_valid() and (not expected or not hmac.compare_digest(expected, request.headers.get('X-Management-CSRF',''))):
            return jsonify(error='页面校验已过期，请刷新'),403


@management_bp.after_request
def no_cache(response):
    response.headers['Cache-Control']='no-store'
    return response


@management_bp.get('/status')
def status():
    session.setdefault('management_csrf',secrets.token_urlsafe(32))
    from ai_review.runtime_status import status as runtime_status
    runtime=runtime_status()
    return jsonify(csrf=session['management_csrf'],runtime=runtime)


@management_bp.get('/ssh-public-key')
def public_key():
    from config import SSH_KEY_PATH
    path=Path(SSH_KEY_PATH+'.pub')
    if not path.is_file(): return jsonify(error='安装器尚未生成管理密钥'),503
    text=path.read_text().strip()
    fingerprint='SHA256:'+base64.b64encode(hashlib.sha256(base64.b64decode(text.split()[1])).digest()).decode().rstrip('=')
    return jsonify(public_key=text,fingerprint=fingerprint)


def remote_parameters(data):
    host=str(data.get('host','')).strip(); user=str(data.get('ssh_user','root')).strip()
    port=int(data.get('ssh_port',22))
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,253}',host) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,63}',user) or not 1<=port<=65535:
        raise ValueError('主机、SSH 用户或端口无效')
    return host,port,user


@management_bp.post('/ssh-fingerprint')
def fingerprint():
    host,port,_=remote_parameters(request.get_json() or {})
    with socket.create_connection((host,port),timeout=5) as sock:
        transport=paramiko.Transport(sock)
        try:
            transport.start_client(timeout=5);key=transport.get_remote_server_key()
            value='SHA256:'+base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
            session['ssh_pending']={'host':host,'port':port,'type':key.get_name(),'key':key.get_base64(),'fingerprint':value,'time':time.time()}
            return jsonify(fingerprint=value)
        finally:transport.close()


@management_bp.post('/ssh-trust')
def trust():
    data=request.get_json() or {}; pending=session.get('ssh_pending')
    if not pending or time.time()-pending['time']>300 or data.get('fingerprint')!=pending['fingerprint']:
        return jsonify(error='主机指纹确认已过期'),409
    host=pending['host'] if pending['port']==22 else f'[{pending["host"]}]:{pending["port"]}'
    path=Path(os.environ['SSH_KNOWN_HOSTS']);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    import fcntl
    with path.open('a+') as f:
        os.chmod(path,0o600);fcntl.flock(f,fcntl.LOCK_EX);f.seek(0)
        existing=f.read()
        matching=[line for line in existing.splitlines() if line.split() and line.split()[0]==host]
        line=f'{host} {pending["type"]} {pending["key"]}'
        if matching and line not in matching:
            response, _ = operation('SSH 主机指纹校验', lambda report: (_ for _ in ()).throw(RuntimeError('已固定的主机指纹发生变化，拒绝覆盖；请核对目标身份')))
            return response,409
        if line not in matching:f.write(line+'\n');f.flush();os.fsync(f.fileno())
    session.pop('ssh_pending',None)
    return jsonify(ok=True)


def containers(data):
    if data.get('mode')=='local':
        with contextlib.closing(docker.from_env(timeout=10)) as client:
            return [c.attrs for c in client.containers.list(all=True)]
    host,port,user=remote_parameters(data)
    if CURRENT.get(): CURRENT.get().event('检查','SSH 接入目标',地址=host,端口=port,账号=user)
    ssh=SSHService(host,port,user)
    code,out,err=ssh.exec("docker ps -aq | xargs -r docker inspect",timeout=20)
    if code:raise RuntimeError('远程 Docker 权限检查失败：'+err)
    return json.loads(out or '[]')


def discover(data):
    rows=containers(data);result=[]
    for row in rows:
        labels=row['Config'].get('Labels') or {}
        if labels.get('com.docker.compose.service')!='gitea':continue
        project=labels.get('com.docker.compose.project')
        peers=[x for x in rows if (x['Config'].get('Labels') or {}).get('com.docker.compose.project')==project and
               (x['Config'].get('Labels') or {}).get('com.docker.compose.service') in ('database','postgres','db')]
        if len(peers)!=1:continue
        ports=row.get('NetworkSettings',{}).get('Ports') or {}
        published=ports.get('3000/tcp') or []
        port=int(published[0]['HostPort']) if published else None
        result.append({'id':row['Id'],'name':row['Name'].lstrip('/'),'project':project,
                       'pg_container':peers[0]['Name'].lstrip('/'),'port':port,
                       'running':row['State']['Running']})
    return result


def operation(title, callback):
    report=Report(diagnostic_root(),title);token=CURRENT.set(report)
    try:
        with report.stage(title):result=callback(report)
        report.finish('成功')
        return jsonify(**result,diagnostic_id=report.id)
    except Exception as exc:
        report.fail(exc);report.finish('失败')
        return jsonify(error=redact(str(exc)),diagnostic_id=report.id),400
    finally:CURRENT.reset(token)


@management_bp.post('/discover')
def discovery():
    data=request.get_json() or {}
    return operation('发现 Gitea 实例',lambda report:{'instances':discover(data)})


def exec_container(data, container, args):
    if data.get('mode')=='local':
        with contextlib.closing(docker.from_env(timeout=30)) as client:
            code,out=client.containers.get(container).exec_run(args,user='git')
            if code:raise RuntimeError('Gitea 初始化失败：'+out.decode(errors='replace')[-1000:])
            return out.decode()
    host,port,user=remote_parameters(data)
    code,out,err=SSHService(host,port,user).exec(shlex.join(['docker','exec','-u','git',container,*args]),timeout=30)
    if code:raise RuntimeError('远程 Gitea 初始化失败：'+err[-1000:])
    return out


@management_bp.post('/connect')
def connect():
    data=request.get_json() or {}
    def run(report):
        selected=next((r for r in discover(data) if r['id']==data.get('instance_id')),None)
        if selected is None:raise ValueError('实例身份已变化，请重新发现')
        identity=selected['project']+':'+selected['name']
        management_host=get_setting('host_ip') if data.get('mode')=='local' else remote_parameters(data)[0]
        if GiteaServer.query.filter_by(instance_identity=identity,host=management_host).first():
            raise ValueError('该实例已添加，请使用已有服务器')
        name=selected['name'];config=configparser.ConfigParser(interpolation=None)
        try:
            config.read_string('[DEFAULT]\n'+exec_container(data,name,['cat','/data/gitea/conf/app.ini']))
        except configparser.Error as exc:
            raise RuntimeError('Gitea 配置格式无效：'+type(exc).__name__) from None
        url=data.get('gitea_url') or config.get('server','ROOT_URL')
        from urllib.parse import urlsplit
        parsed=urlsplit(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Gitea 访问地址无效')
        # 由已授权的管理通道创建独立令牌，不要求用户复制初始管理员密码。
        token_name='manager-'+uuid.uuid4().hex
        prefix=['gitea','--config','/data/gitea/conf/app.ini','admin','user']
        token=exec_container(data,name,prefix+['generate-access-token','--username','administrator','--token-name',token_name,'--scopes','all','--raw']).strip()
        register_secret(token)
        server=GiteaServer(name=data.get('name') or selected['project'],role=data.get('role','primary'),
            host=management_host,ssh_port=int(data.get('ssh_port',22)),ssh_user=data.get('ssh_user','root'),
            connection_mode='local' if data.get('mode')=='local' else 'ssh',is_local=data.get('mode')=='local',
            instance_identity=identity,gitea_container=name,pg_container=selected['pg_container'],
            pg_dbname=config.get('database','NAME'),pg_user=config.get('database','USER'),
            gitea_port=selected['port'] or parsed.port or (443 if parsed.scheme=='https' else 80),gitea_url=url,api_token=token)
        import requests
        with report.stage('验证管理 API 身份'):
            s=requests.Session();s.trust_env=False
            response=s.get(url.rstrip('/')+'/api/v1/user',headers={'Authorization':'token '+token},timeout=10,allow_redirects=False)
            s.close()
            if response.status_code!=200 or not response.json().get('is_admin'):
                raise RuntimeError('管理 API 身份验证失败，HTTP '+str(response.status_code))
        server.status='online';db.session.add(server);db.session.commit()
        return {'id':server.id,'ok':True}
    return operation('添加并验证服务器',run)


@management_bp.get('/diagnostics')
def reports():
    result=[]
    for kind,root in [('operation',diagnostic_root()),('installation',Path(os.environ.get('INSTALL_DIAGNOSTICS','/app/install-diagnostics')))]:
        if not root.exists():continue
        for p in sorted(root.glob('*/status.json'),reverse=True)[:10]:
            try:result.append({'id':p.parent.name,'kind':kind,**json.loads(p.read_text())})
            except (OSError,ValueError):continue
    return jsonify(reports=result)


@management_bp.get('/diagnostics/<kind>/<ident>')
def download_report(kind,ident):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',ident) or kind not in ('operation','installation'):
        return jsonify(error='诊断编号无效'),404
    root=diagnostic_root() if kind=='operation' else Path(os.environ.get('INSTALL_DIAGNOSTICS','/app/install-diagnostics'))
    path=root/('diagnostics-'+ident+'.tar.gz')
    if not path.is_file():return jsonify(error='诊断包不存在或尚未完成'),404
    return send_file(path,as_attachment=True,download_name=path.name)


@management_bp.errorhandler(Exception)
def error(exc):
    from werkzeug.exceptions import HTTPException
    from access.errors import AccessError
    if isinstance(exc, AccessError):
        db.session.rollback()
        return jsonify(error=str(exc)), exc.status
    if isinstance(exc,HTTPException):return exc
    return operation('管理接口失败',lambda report:(_ for _ in ()).throw(exc))


@management_bp.get('/diagnostics/<kind>/<ident>/steps')
def report_steps(kind, ident):
    if kind not in ('operation','installation') or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',ident):
        return jsonify(error='诊断编号无效'),404
    root=diagnostic_root() if kind=='operation' else Path(os.environ.get('INSTALL_DIAGNOSTICS','/app/install-diagnostics'))
    path=root/ident/'events.jsonl'
    if not path.is_file(): return jsonify(events=[])
    events=[]
    for line in path.read_text()[:2*1024*1024].splitlines():
        try:events.append(json.loads(line))
        except ValueError:events.append({'状态':'截断','说明':'该条记录未写完整'})
    return jsonify(events=events)
