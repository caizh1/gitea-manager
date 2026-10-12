"""核验 Gitea 创建时间，阻止外部重建后复用数字 ID 继承旧授权或历史。"""
import json
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from sqlalchemy.dialects.sqlite import insert

from models import db
from access.errors import AccessError


class OrganizationIdentity(db.Model):
    __tablename__ = 'access_organization_identities'
    binding_id = db.Column(db.Integer, primary_key=True)
    created = db.Column(db.String(40), nullable=False)


class RepositoryIdentity(db.Model):
    __tablename__ = 'access_repository_identities'
    server_id = db.Column(db.Integer, primary_key=True)
    instance_epoch = db.Column(db.String(200), primary_key=True)
    repo_id = db.Column(db.Integer, primary_key=True)
    created = db.Column(db.String(40), nullable=False)


def created_value(value):
    if not isinstance(value, str):
        raise AccessError('Gitea 未返回可核验的创建时间，已停止组织访问', 409)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except ValueError:
        raise AccessError('Gitea 创建时间格式无效，已停止组织访问', 409) from None


def organization_birth(client, name, organization_id):
    user = client.request('GET', '/users/' + quote(name, safe=''), allow_missing=True)
    if not user or user.get('id') != organization_id:
        raise AccessError('Gitea 组织与用户身份不一致，请重新核验实例', 409)
    return created_value(user.get('created'))


def verify_organization(binding, created, require_record=True):
    record = db.session.get(OrganizationIdentity, binding.id, populate_existing=True)
    if not record:
        if require_record:
            raise AccessError('组织尚未登记创建身份，请联系平台管理员重新同步', 409)
        return
    if record.created != created:
        from access.scopes import invalidate_server
        from models import GiteaServer
        invalidate_server(db.session.get(GiteaServer, binding.server_id))
        raise AccessError('检测到 Gitea 组织数字 ID 被复用，实例授权已失效，请重新同步和授权', 409)


def register_organization(binding, created):
    """仅由平台组织同步登记；已有创建身份不能被同步悄然覆盖。"""
    verify_organization(binding, created, require_record=False)
    db.session.execute(insert(OrganizationIdentity).values(binding_id=binding.id, created=created)
                       .on_conflict_do_nothing(index_elements=['binding_id']))


def verify_repository(binding, repository, commit=True):
    """初次记录身份；重建则封存原快照并暂停当前请求，刷新后可显式创建新资源。"""
    from access.scopes import ResourceScope, freeze_review_resources
    repo_id = repository.get('id')
    if type(repo_id) is not int or repo_id < 1:
        raise AccessError('Gitea 仓库稳定身份无效', 409)
    created = created_value(repository.get('created_at'))
    key = {'server_id': binding.server_id, 'instance_epoch': binding.instance_epoch, 'repo_id': repo_id}
    record = db.session.get(RepositoryIdentity, (binding.server_id, binding.instance_epoch, repo_id), populate_existing=True)
    inserted = record is None
    if inserted:
        db.session.execute(insert(RepositoryIdentity).values(**key, created=created).on_conflict_do_nothing())
        record = db.session.get(RepositoryIdentity, (binding.server_id, binding.instance_epoch, repo_id), populate_existing=True)
    if record.created == created:
        if inserted and commit:
            db.session.commit()
        return inserted
    changed = db.session.execute(db.update(RepositoryIdentity).filter_by(**key, created=record.created)
                                 .values(created=created)).rowcount
    archived = []
    if changed:
        for scope in ResourceScope.query.filter_by(server_id=binding.server_id,
                instance_epoch=binding.instance_epoch, archived=False):
            if repo_id in json.loads(scope.repo_ids_json):
                scope.archived = True
                archived.append(scope)
        # 这两类旧表保存当前状态而非 ResourceScope；清除组织可见关联，保留原始记录给平台核对。
        from models import CommitGateAssignment
        from access.statistics import RepositoryStatisticsSnapshot
        CommitGateAssignment.query.filter_by(server_id=binding.server_id, repo_id=repo_id).update({'repo_id': 0})
        RepositoryStatisticsSnapshot.query.filter_by(server_id=binding.server_id, repo_id=repo_id,
            instance_epoch=binding.instance_epoch).update({'instance_epoch': 'archived:' + uuid.uuid4().hex})
    db.session.commit()
    freeze_review_resources(archived)
    raise AccessError('检测到仓库数字 ID 被复用，旧配置和历史已封存，请刷新后重新配置', 409)
