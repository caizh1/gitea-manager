"""迁移中心的数据校验和短事务入口，不在 HTTP 请求中转换仓库。"""
import csv
from datetime import datetime
import io
import json
import re
import time
from sqlalchemy.exc import IntegrityError

from models import db, GiteaServer
from .common import (MigrationError, require, string, slug, source_url, within_source,
                     repo_name, layout, encrypt, decrypt, encode, digest, work_root, file_lock)
from .gitea import target_fingerprint
from .models import (SvnSource, SvnRepository, SvnMigrationPlan, SvnMigrationRun,
                     SvnMigrationItem, SvnMigrationEvent, SvnReservation, SvnWorkerHealth)

LIVE = ('queued', 'running', 'paused', 'uncertain')
LABELS = {'draft': '草稿', 'queued': '排队中', 'preflight': '预检中', 'blocked': '待处理', 'ready': '可执行',
          'running': '执行中', 'pending': '等待执行', 'paused': '已暂停', 'uncertain': '待确认',
          'success': '成功', 'partial': '部分完成', 'failed': '失败', 'cancelled': '已取消'}


def public_source(row):
    return {'id': row.id, 'slug': row.slug, 'name': row.name, 'url': row.url, 'username': row.username,
            'password_set': bool(row.password_ciphertext), 'ca_pem': row.ca_pem, 'version': row.version,
            'archived': row.archived, 'test': json.loads(row.test_json),
            'tested_at': row.tested_at.isoformat() if row.tested_at else None}


def public_repo(row):
    return {'id': row.id, 'source_id': row.source_id, 'url': row.url, 'name': row.name,
            'layout': json.loads(row.layout_json), 'version': row.version, 'archived': row.archived,
            'inventory': json.loads(row.inventory_json)}


def public_plan(row):
    from .preflights import latest_for_plans, public
    latest = latest_for_plans([row.id]).get(row.id)
    return {'id': row.id, 'name': row.name, 'server_id': row.server_id, 'config': json.loads(row.config_json),
            'version': row.version, 'status': row.status, 'status_label': LABELS.get(row.status, row.status),
            'preview': public_preview(json.loads(row.preview_json)), 'preview_hash': row.preview_hash, 'error': row.error,
            'preflight': public(latest) if latest else None}


def public_preview(value):
    return dict(value, repositories=[{key: item for key, item in row.items() if key not in ('history', 'properties')}
                                     for row in value.get('repositories', [])])


def public_run(row, detail=False, report=False):
    snapshot = json.loads(row.snapshot_json)
    result = {'id': row.id, 'plan_id': row.plan_id, 'server_id': row.server_id, 'status': row.status,
              'plan_name': snapshot.get('plan_name', ''),
              'plan_version': snapshot.get('inputs', {}).get('plan_version'),
              'preflight_id': snapshot.get('preflight_id'),
              'status_label': LABELS.get(row.status, row.status), 'control': row.control,
              'actor_name': row.actor_name, 'error': row.error, 'created_at': row.created_at.isoformat(),
              'updated_at': row.updated_at.isoformat()}
    items = SvnMigrationItem.query.filter_by(run_id=row.id).order_by(SvnMigrationItem.position).all()
    result['completed'] = sum(i.status == 'success' for i in items)
    result['total'] = len(items)
    if detail:
        result['snapshot'] = public_preview(snapshot)
        result['items'] = [{'id': i.id, 'repository_id': i.repository_id, 'position': i.position,
                            'status': i.status, 'status_label': LABELS.get(i.status, i.status), 'stage': i.stage,
                            'detail': i.detail, 'result': {k: v for k, v in json.loads(i.result_json).items()
                                                         if report or k not in ('提交映射', '属性', '修订覆盖')}} for i in items]
    return result


def get(model, ident):
    row = db.session.get(model, ident)
    require(row is not None, '记录不存在', 'not_found', 404)
    return row


def expected_version(row, data):
    require(data.get('version') == row.version, '记录已经变化，请刷新后修改', 'stale', 409)


def source_edit(data, ident=None):
    require(isinstance(data, dict), '来源格式无效')
    row = get(SvnSource, ident) if ident else SvnSource()
    if ident:
        expected_version(row, data)
        # 来源凭据正在使用时禁止变更，避免把两个实例的数据混在一次转换里。
        repo_ids = [r.id for r in SvnRepository.query.filter_by(source_id=row.id)]
        require(not SvnMigrationItem.query.join(SvnMigrationRun, SvnMigrationItem.run_id == SvnMigrationRun.id).filter(
            SvnMigrationItem.repository_id.in_(repo_ids), SvnMigrationRun.status.in_(LIVE)).first(), '该来源仍有未完成迁移，暂不能修改', 'busy', 409)
    selected_slug = slug(data.get('slug'))
    require(not ident or selected_slug == row.slug, '来源英文标识创建后不能修改')
    row.slug = selected_slug
    row.name = string(data.get('name'), '来源名称')
    row.url = source_url(data.get('url'))
    row.username = string(data.get('username'), 'SVN 读取账号')
    password = data.get('password', '')
    require(isinstance(password, str) and len(password) <= 4096 and not any(c in password for c in '\x00\r\n'), '密码格式无效')
    if password:
        row.password_ciphertext = encrypt(password)
    row.ca_pem = data.get('ca_pem', '')
    require(isinstance(row.ca_pem, str) and len(row.ca_pem) <= 128000, 'CA 证书格式无效')
    if row.ca_pem:
        import ssl
        try:
            ssl.create_default_context(cadata=row.ca_pem)
        except (ssl.SSLError, ValueError):
            raise MigrationError('CA 证书无法读取') from None
    row.archived = data.get('archived', False) is True
    row.version = row.version + 1 if ident else 1
    row.test_json = '{}'
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise MigrationError('来源英文标识已存在', 'conflict', 409) from None
    return row


def source_runtime(ident, version=None):
    row = get(SvnSource, ident)
    require(not row.archived and (version is None or row.version == version), 'SVN 来源已停用或配置发生变化', 'stale', 409)
    return {'id': row.id, 'name': row.name, 'slug': row.slug, 'url': row.url, 'username': row.username,
            'password': decrypt(row.password_ciphertext), 'ca_pem': row.ca_pem, 'version': row.version}


def edit_repository(source_id, data, ident=None, commit=True):
    source = get(SvnSource, source_id)
    require(not source.archived, '来源已停用')
    row = get(SvnRepository, ident) if ident else SvnRepository(source_id=source_id)
    require(row.source_id == source_id, '仓库不属于此来源')
    if ident:
        expected_version(row, data)
        require(not SvnReservation.query.filter_by(key='source:' + str(ident)).first(), '仓库仍有未完成的迁移', 'busy', 409)
    row.url = source_url(data.get('url'))
    require(within_source(row.url, source.url), '仓库 URL 必须位于所选来源地址下')
    row.name = repo_name(data.get('name'))
    if data.get('detection_id') is not None:
        from .layout_detection import confirmed
        selected_layout = confirmed(source, data)
    else:
        selected_layout = layout(data.get('layout', {}))
    row.layout_json = encode(selected_layout)
    row.archived = data.get('archived', False) is True
    row.version = row.version + 1 if ident else 1
    db.session.add(row)
    try:
        db.session.commit() if commit else db.session.flush()
    except IntegrityError:
        db.session.rollback()
        raise MigrationError('该来源已经登记此仓库 URL', 'conflict', 409) from None
    return row


def authors_config(value):
    if isinstance(value, str):
        require(len(value) <= 4 * 1024 * 1024, '作者清单过大')
        try:
            value = list(csv.DictReader(io.StringIO(value.lstrip('\ufeff'))))
        except csv.Error:
            raise MigrationError('作者 CSV 格式无效') from None
    require(isinstance(value, list) and len(value) <= 100000, '作者映射须为数组或 CSV')
    result, seen = [], set()
    for row in value:
        require(isinstance(row, dict), '作者行格式无效')
        try:
            source_id = int(row.get('source_id'))
        except (ValueError, TypeError):
            raise MigrationError('作者行需要来源 ID') from None
        user = string(row.get('svn_user'), 'SVN 作者', 300)
        name = string(row.get('name'), '作者姓名', 300)
        email = string(row.get('email'), '作者邮箱', 320)
        require(not any(c in user for c in '=<>') and not any(c in name for c in '<>') and re.fullmatch(r'[^\s<>@]+@[^\s<>@]+', email), '作者姓名或邮箱格式无效')
        key = (source_id, user)
        require(key not in seen, '作者映射中存在重复来源和用户名')
        seen.add(key)
        result.append({'source_id': source_id, 'svn_user': user, 'name': name, 'email': email})
    return result


def plan_edit(data, ident=None):
    row = get(SvnMigrationPlan, ident) if ident else SvnMigrationPlan()
    if ident:
        expected_version(row, data)
        require(row.status not in ('queued', 'preflight'), '计划正在预检，暂不能修改', 'busy', 409)
    row.name = string(data.get('name'), '计划名称')
    server = get(GiteaServer, data.get('server_id'))
    rows = data.get('repositories')
    require(isinstance(rows, list) and 0 < len(rows) <= 1000, '请选择 1 至 1000 个仓库')
    ids, selected = set(), []
    for item in rows:
        require(isinstance(item, dict) and isinstance(item.get('id'), int), '仓库选择无效')
        repo = get(SvnRepository, item['id'])
        require(repo.id not in ids and not repo.archived, '仓库重复或已经停用')
        ids.add(repo.id)
        selected.append({'id': repo.id, 'name': repo_name(item.get('name', repo.name))})
    ca = data.get('target_ca_pem', '')
    require(isinstance(ca, str) and len(ca) <= 128000, '目标 CA 无效')
    old_config = json.loads(row.config_json) if ident else {}
    target_group = data.get('target_group', old_config.get('target_group'))
    # 旧客户端和历史计划保留原规则；新版页面总是明确传入目标群组。
    if target_group is not None:
        require(isinstance(target_group, dict) and set(target_group) == {'id'}
                and type(target_group['id']) is int and target_group['id'] > 0, '目标群组选择无效')
    row.config_json = encode({'repositories': selected, 'authors': authors_config(data.get('authors', [])),
                              'target_ca_pem': ca, 'excluded': data.get('excluded', []),
                              **({'target_group': target_group} if target_group else {})})
    row.server_id, row.status, row.preview_hash, row.preview_json, row.error = server.id, 'draft', '', '{}', ''
    row.version = row.version + 1 if ident else 1
    db.session.add(row); db.session.commit()
    return row


def author_map(source_id, authors, configured):
    result = {}
    provided = {r['svn_user']: r for r in configured if r['source_id'] == source_id}
    for user in authors:
        require(not any(c in user for c in '\r\n\x00=<>'), 'SVN 作者含无法安全映射的字符，请先修正来源身份', 'author')
        code = digest([source_id, user])[:24]
        result[user] = {'name': user, 'email': 'svn-' + code + '@history.invalid', 'placeholder': True}
        if user in provided:
            result[user] = {'name': provided[user]['name'], 'email': provided[user]['email'], 'placeholder': False}
    return result


def input_state(plan):
    config = json.loads(plan.config_json)
    result = {'plan_version': plan.version, 'target': target_fingerprint(get(GiteaServer, plan.server_id)), 'repositories': []}
    for choice in config['repositories']:
        repo = get(SvnRepository, choice['id'])
        source = get(SvnSource, repo.source_id)
        require(not repo.archived and not source.archived, '来源或仓库已停用', 'stale', 409)
        result['repositories'].append([repo.id, repo.version, source.id, source.version])
    return result


def queue_preflight(plan):
    with file_lock(work_root() / 'enqueue.lock', blocking=True):
        db.session.refresh(plan)
        require(plan.status not in ('queued', 'preflight'), '计划已经在预检', 'busy', 409)
        from .preflights import begin
        begin(plan)
        plan.status, plan.preview_json, plan.preview_hash, plan.error = 'queued', '{}', '', ''
        db.session.commit()


def create_run(data, actor):
    key = string(data.get('request_key'), '幂等标识', 100)
    with file_lock(work_root() / 'enqueue.lock', blocking=True):
        old = SvnMigrationRun.query.filter_by(request_key=key).first()
        if old:
            require(old.plan_id == data.get('plan_id') and old.actor_id == actor.id and
                    json.loads(old.snapshot_json).get('preflight_hash') == data.get('preview_hash'),
                    '幂等标识已经用于其他请求', 'conflict', 409)
            return old
        plan = get(SvnMigrationPlan, data.get('plan_id'))
        require(plan.status == 'ready' and plan.preview_hash and plan.preview_hash == data.get('preview_hash'), '预览已过期或未通过', 'stale', 409)
        preview = json.loads(plan.preview_json)
        require(preview['inputs'] == input_state(plan), '输入已变化，请重新预检', 'stale', 409)
        require(data.get('freeze_confirmed') is True and data.get('full_read_confirmed') is True, '请确认 SVN 已备份停写，且迁移账号具有完整历史读取权限')
        from .preflights import latest_for_plans
        checked = latest_for_plans([plan.id]).get(plan.id)
        snapshot = dict(preview, plan_name=plan.name, preflight_id=checked.id if checked else None, freeze={'confirmed': True, 'at': datetime.utcnow().isoformat(), 'actor': actor.display_name or actor.username},
                        full_read_confirmed=True, preflight_hash=plan.preview_hash)
        run = SvnMigrationRun(plan_id=plan.id, server_id=plan.server_id, request_key=key, snapshot_json=encode(snapshot),
                              actor_id=actor.id, actor_name=actor.display_name or actor.username)
        db.session.add(run); db.session.flush()
        for position, item in enumerate(preview['repositories']):
            db.session.add(SvnMigrationItem(run_id=run.id, repository_id=item['repository_id'], position=position))
            for reservation in ('source:' + str(item['repository_id']), 'target:' + str(plan.server_id) + ':' + item['owner'].lower() + '/' + item['name'].lower()):
                db.session.add(SvnReservation(key=reservation, run_id=run.id))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise MigrationError('来源仓库或目标已被其他未完成任务占用', 'conflict', 409) from None
        return run


def release_reservations(run):
    SvnReservation.query.filter_by(run_id=run.id).delete()


def control_run(run, action):
    with file_lock(work_root() / 'enqueue.lock', blocking=True):
        db.session.refresh(run)
        if action == 'pause':
            require(run.status in ('queued', 'running'), '当前状态不能暂停', 'state', 409)
            if run.status == 'queued':
                run.status = 'paused'
            else:
                run.control = 'pause'
        elif action in ('resume', 'reconcile'):
            require(run.status in ('paused', 'failed', 'partial', 'uncertain', 'cancelled'), '当前状态不能恢复', 'state', 409)
            # 失败、取消的任务已释放占用，恢复前重新原子占用全部来源和目标。
            snapshot = json.loads(run.snapshot_json)
            for item in snapshot['repositories']:
                for key in ('source:' + str(item['repository_id']), 'target:' + str(run.server_id) + ':' + item['owner'].lower() + '/' + item['name'].lower()):
                    existing = db.session.get(SvnReservation, key)
                    require(not existing or existing.run_id == run.id, '恢复目标已被其他任务占用', 'conflict', 409)
                    if not existing:
                        db.session.add(SvnReservation(key=key, run_id=run.id))
            run.status, run.control = 'queued', ''
        elif action == 'cancel':
            require(run.status in LIVE, '当前状态不能取消', 'state', 409)
            if run.status == 'running':
                run.control = 'cancel'
            else:
                run.status, run.control = 'cancelled', ''
                release_reservations(run)
        else:
            raise MigrationError('未知任务操作')
        run.updated_at = datetime.utcnow()
        db.session.commit()
    return run


def event(run, item, stage, message):
    if item:
        item.stage, item.detail = stage, message
    run.updated_at = datetime.utcnow()
    db.session.add(SvnMigrationEvent(run_id=run.id, item_id=item.id if item else None, stage=stage, message=message))
    db.session.commit()


def health():
    row = db.session.get(SvnWorkerHealth, 1)
    return {'online': bool(row and time.time() - row.updated < 30), 'updated': row.updated if row else None}
