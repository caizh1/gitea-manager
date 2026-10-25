"""来源凭据保管、乐观锁与幂等排队。"""
import json
import os
import re
import ssl

from cryptography.fernet import Fernet, InvalidToken
from models import db
from svn_migration.common import MigrationError, data_root, file_lock, require, source_url, string
from .models import GitMigrationSource, GitMigrationDiscovery, GitMigrationInventory, GitMigrationRun


def root():
    path = data_root() / 'git-migrations'
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def cipher(create=False):
    path = data_root() / 'git-migration.key'
    with file_lock(root() / 'key.lock', blocking=True):
        if not path.exists():
            require(create and not GitMigrationSource.query.first(),
                    'Git 迁移原密钥缺失，请恢复原密钥，不能重新生成替代', 'secret', 503)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
        try:
            value = Fernet(path.read_bytes().strip())
            existing = GitMigrationSource.query.first()
            if existing:
                value.decrypt(existing.secret_ciphertext.encode())
            return value
        except (ValueError, OSError, InvalidToken):
            raise MigrationError('Git 迁移密钥损坏，请恢复原密钥', 'secret', 503) from None


def decrypt(value):
    try:
        return cipher().decrypt(value.encode()).decode()
    except (InvalidToken, UnicodeError):
        raise MigrationError('Git 迁移凭据无法解密，请恢复原密钥', 'secret', 503) from None


def get(model, ident):
    row = db.session.get(model, ident)
    require(row is not None, '记录不存在', 'not_found', 404)
    return row


def public_source(row):
    return {'id': row.id, 'name': row.name, 'kind': row.kind, 'url': row.url,
            'username': row.username, 'auth_method': row.auth_method, 'secret_set': bool(row.secret_ciphertext),
            'ca_pem': row.ca_pem, 'version': row.version, 'archived': row.archived}


def public_discovery(row, detail=True):
    source = db.session.get(GitMigrationSource, row.source_id)
    result = {'id': row.id, 'source_id': row.source_id, 'source_version': row.source_version,
              'stale': source is None or source.version != row.source_version,
              'status': row.status, 'error': row.error, 'error_code': row.error_code,
              'created_at': row.created_at.isoformat() + 'Z',
              'completed_at': row.completed_at.isoformat() + 'Z' if row.completed_at else None}
    if detail:
        result['result'] = json.loads(row.result_json)
        if isinstance(row, GitMigrationInventory):
            # 页面只读取摘要，避免每次轮询传输完整评论与作业正文。
            for project in result['result'].get('projects', []):
                for dataset in project.get('datasets', []):
                    dataset['object_count'] = len(dataset.pop('rows', []))
    if isinstance(row, GitMigrationInventory):
        result.update(discovery_id=row.discovery_id, project_ids=json.loads(row.project_ids_json),
                      capture_code=row.capture_code)
    return result


def busy(source_id):
    return any(model.query.filter_by(source_id=source_id).filter(
        model.status.in_(('queued', 'running', 'interrupted', 'uncertain', 'quarantined')) if model is GitMigrationRun else model.status.in_(('queued', 'running'))).first() is not None
        for model in (GitMigrationDiscovery, GitMigrationInventory, GitMigrationRun))


def save_source(data, ident=None):
    allowed = {'name', 'kind', 'url', 'username', 'auth_method', 'secret', 'ca_pem', 'version', 'archived'}
    require(isinstance(data, dict) and not set(data) - allowed, '来源包含不支持的字段')
    row = get(GitMigrationSource, ident) if ident else GitMigrationSource()
    if ident:
        require(type(data.get('version')) is int and data['version'] == row.version, '来源已变化，请刷新后修改', 'stale', 409)
        require(not busy(ident), '来源正在发现或盘点，请等待完成后修改', 'busy', 409)
    kind = data.get('kind')
    method = data.get('auth_method', 'password')
    require(kind in ('gitlab', 'gitea') and method in ('password', 'token'), '来源平台或认证方式无效')
    name = string(data.get('name'), '来源名称')
    username = string(data.get('username', ''), '管理员账号', empty=method == 'token')
    url = source_url(data.get('url'), ('http', 'https'))
    ca = data.get('ca_pem', '')
    require(isinstance(ca, str) and len(ca) <= 128000, '自定义证书格式无效')
    if ca:
        try:
            ssl.create_default_context(cadata=ca)
        except (ssl.SSLError, ValueError):
            raise MigrationError('自定义 CA 不是有效 PEM 证书') from None
    secret = data.get('secret', '')
    require(isinstance(secret, str) and len(secret) <= 8192 and not any(c in secret for c in '\r\n\x00'), '凭据格式无效')
    if ident and (row.url != url or row.kind != kind or row.auth_method != method or row.username != username):
        require(bool(secret), '来源地址、账号或认证方式变化时必须重新提供凭据')
    require(bool(secret) or bool(row.secret_ciphertext), '请提供来源密码或访问令牌')
    archived = data.get('archived', row.archived if ident else False)
    require(type(archived) is bool, '归档标记必须为布尔值')
    if secret:
        row.secret_ciphertext = cipher(create=True).encrypt(secret.encode()).decode()
    row.name, row.username, row.kind, row.url = name, username, kind, url
    row.auth_method, row.ca_pem, row.archived = method, ca, archived
    db.session.add(row)
    db.session.commit()
    return row


def enqueue_discovery(source_id, data, actor_id):
    require(isinstance(data, dict) and set(data) <= {'source_version', 'request_key'}, '发现请求字段无效')
    key = data.get('request_key')
    require(isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_-]{16,80}', key), '请求标识无效')
    with file_lock(root() / 'enqueue.lock', blocking=True):
        db.session.expire_all()
        source = get(GitMigrationSource, source_id)
        existing = GitMigrationDiscovery.query.filter_by(request_key=key).first()
        if existing:
            require(existing.source_id == source.id and existing.source_version == data.get('source_version')
                    and existing.actor_id == actor_id, '请求标识已用于其他输入', 'conflict', 409)
            return existing
        require(not source.archived, '来源已归档')
        require(type(data.get('source_version')) is int and data['source_version'] == source.version,
                '来源已变化，请刷新后重试', 'stale', 409)
        require(not busy(source_id), '此来源已有发现或盘点任务，请查看现有任务', 'busy', 409)
        row = GitMigrationDiscovery(source_id=source.id, source_version=source.version,
                                    request_key=key, actor_id=actor_id)
        db.session.add(row)
        db.session.commit()
        return row


def enqueue_inventory(source_id, data, actor_id):
    require(isinstance(data, dict) and {'discovery_id', 'project_ids', 'request_key'} <= set(data)
            and set(data) <= {'discovery_id', 'project_ids', 'request_key', 'capture_code'}, '盘点请求字段无效')
    capture_code = data.get('capture_code', False)
    require(type(capture_code) is bool, '代码核验选项必须为布尔值')
    key, ids, discovery_id = data['request_key'], data['project_ids'], data['discovery_id']
    require(isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_-]{16,80}', key), '请求标识无效')
    require(isinstance(ids, list) and 1 <= len(ids) <= 100 and all(type(i) is int and i > 0 for i in ids)
            and len(set(ids)) == len(ids), '每次选择一至一百个不同项目')
    require(type(discovery_id) is int, '发现记录编号无效')
    ids_json = json.dumps(sorted(ids))
    with file_lock(root() / 'enqueue.lock', blocking=True):
        db.session.expire_all()
        source = get(GitMigrationSource, source_id)
        existing = GitMigrationInventory.query.filter_by(request_key=key).first()
        if existing:
            require(existing.source_id == source_id and existing.discovery_id == discovery_id
                    and existing.project_ids_json == ids_json and existing.actor_id == actor_id
                    and existing.capture_code == capture_code,
                    '请求标识已用于其他输入', 'conflict', 409)
            return existing
        discovery = get(GitMigrationDiscovery, discovery_id)
        require(not source.archived and discovery.source_id == source_id and discovery.status == 'succeeded'
                and discovery.source_version == source.version, '发现记录已失效，请重新发现项目', 'stale', 409)
        projects = json.loads(discovery.result_json).get('projects', [])
        require(set(ids) <= {p['id'] for p in projects}, '所选项目不属于这次发现结果')
        require(not busy(source_id), '此来源已有发现或盘点任务', 'busy', 409)
        row = GitMigrationInventory(source_id=source_id, source_version=source.version, discovery_id=discovery_id,
                                    project_ids_json=ids_json, request_key=key, actor_id=actor_id, capture_code=capture_code)
        db.session.add(row)
        db.session.commit()
        return row
