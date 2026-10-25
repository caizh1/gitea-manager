"""独立串行 Worker；数据库只保存短事务，转换子进程由进程组管理。"""
import argparse
import contextlib
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import time

from flask import Flask
from models import db, GiteaServer
from .common import (MigrationError, Uncertain, Cancelled, require, encode, digest, work_root,
                     file_lock, atomic_json)
from .engine import Process, Source, convert, verify_history, reap_processes, strip_checkout
from .gitea import Gitea, target_fingerprint
from .guards import target_guard
from .models import (SvnSource, SvnRepository, SvnTargetBinding, SvnMigrationPlan, SvnMigrationRun,
                     SvnMigrationItem, SvnWorkerHealth)
from .service import (source_runtime, author_map, input_state, event, release_reservations,
                      public_run, get)

STOP = False


def heartbeat():
    row = db.session.get(SvnWorkerHealth, 1)
    if row is None:
        row = SvnWorkerHealth(id=1)
        db.session.add(row)
    row.updated = time.time()
    db.session.commit()


def check_tick(run_id=None):
    if STOP:
        raise Cancelled()
    from services.upgrade_maintenance import active
    if active():
        raise MigrationError('升级维护中，已停止迁移并保留检查点', 'maintenance')
    if run_id:
        row = db.session.get(SvnMigrationRun, run_id, populate_existing=True)
        if row.control == 'cancel':
            raise Cancelled()
        from access.models import ManagerAccount
        account = db.session.get(ManagerAccount, row.actor_id, populate_existing=True)
        require(account and account.enabled and account.is_platform_admin, '发起人的平台管理权限已经失效，已停止迁移', 'authorization', 403)
    last = getattr(check_tick, 'last', 0)
    if time.monotonic() - last > 5:
        heartbeat()
        check_tick.last = time.monotonic()


def select_group(api, source, server_id, actor):
    bindings = SvnTargetBinding.query.filter_by(source_id=source['id'], server_id=server_id).order_by(SvnTargetBinding.id.desc()).all()
    for binding in bindings:
        if binding.actor_id != actor['id']:
            continue
        try:
            api.group_private(binding.group_id, binding.group_path, actor['id'])
            return {'owner': binding.group_path, 'group_id': binding.group_id, 'group_name': ''}
        except MigrationError as exc:
            if exc.code not in ('privacy', 'gitea'):
                raise
    for number in range(1, 10001):
        path = 'svn-' + source['slug'] + ('' if number == 1 else '-' + str(number))
        if api.api('GET', '/orgs/' + path, missing=True) is None:
            return {'owner': path, 'group_id': None, 'group_name': source['name'] + ' SVN 迁移 ' + secrets.token_hex(8)}
    raise MigrationError('没有可用的私有群组名称')


def preflight(plan_id):
    plan = get(SvnMigrationPlan, plan_id)
    from .models import SvnPreflight
    from .preflights import begin, finish
    checked = SvnPreflight.query.filter_by(plan_id=plan.id, status='queued').order_by(SvnPreflight.id.desc()).first() or begin(plan)
    checked.status = 'preflight'
    plan.status, plan.error = 'preflight', ''
    db.session.commit()
    version = plan.version
    folder = work_root() / ('preview-' + str(plan.id) + '-' + str(version))
    folder.mkdir(mode=0o700, exist_ok=True)
    result = {'repositories': [], 'issues': [], 'authors': [], 'excluded': [], 'inputs': {}, 'created_at': datetime.utcnow().isoformat()}
    api = None
    try:
        config = json.loads(plan.config_json)
        result['excluded'] = config.get('excluded', [])
        result['inputs'] = input_state(plan)
        result['selected'] = [dict(repository_id=r.id, source_id=r.source_id,
            source_name=get(SvnSource, r.source_id).name, name=r.name, url=r.url, layout=json.loads(r.layout_json))
            for r in (get(SvnRepository, c['id']) for c in config['repositories'])]
        result['target_ca_pem'] = config['target_ca_pem']
        with __import__('tempfile').TemporaryDirectory(dir=folder, prefix='credentials-') as secret_dir:
            process = Process(folder, check_tick)
            api = Gitea(get(GiteaServer, plan.server_id), process, secret_dir, config['target_ca_pem'])
            result['actor'] = api.check()
            selected_group = api.selected_scope(config['target_group']['id']) if config.get('target_group') else None
            if selected_group:
                result['target_group'] = selected_group
            occupied_group_paths = api.child_group_paths(selected_group['id']) if selected_group else set()
            groups, destinations, maps = {}, set(), {}
            for choice in config['repositories']:
                repo = get(SvnRepository, choice['id'])
                try:
                    source = source_runtime(repo.source_id)
                    process.secrets = (source['password'], api.token)
                    svn = Source(source, process, secret_dir)
                    inventory = svn.inventory(repo.url, json.loads(repo.layout_json))
                    repo.inventory_json = encode({'uuid': inventory['uuid'], 'revision': inventory['revision'],
                                                  'authors': inventory['authors'], 'branches': inventory['branches'], 'tags': inventory['tags']})
                    # 盘点结果先短事务落盘，避免查询绑定时自动刷新后跨 Gitea 请求持有写锁。
                    db.session.commit()
                    if source['id'] not in groups:
                        groups[source['id']] = ({'owner': selected_group['path'], 'group_id': selected_group['id'],
                                                'group_name': selected_group['name'], 'target_group': selected_group}
                                               if selected_group else select_group(api, source, plan.server_id, result['actor']))
                    target = groups[source['id']]
                    destination = target['owner'].lower() + '/' + choice['name'].lower()
                    require(destination not in destinations, '多个来源路径映射到同一目标仓库', 'conflict')
                    require(destination.casefold() not in occupied_group_paths, '目标路径已被子群组占用', 'conflict')
                    destinations.add(destination)
                    require(api.repository(target['owner'], choice['name'], missing=True) is None, '目标仓库已经存在，不能覆盖', 'conflict')
                    identities = author_map(source['id'], inventory['authors'], config['authors'])
                    maps.setdefault(source['id'], {}).update(identities)
                    result['repositories'].append(dict(inventory, **target, name=choice['name'], repository_id=repo.id,
                        source_id=source['id'], source_version=source['version'], source_name=source['name'], repository_version=repo.version,
                        authors_map=identities))
                except MigrationError as exc:
                    result['issues'].append({'repository_id': repo.id, 'url': repo.url, 'code': exc.code, 'message': str(exc)})
                db.session.commit()
            result['authors'] = [dict(value, source_id=ident, svn_user=user) for ident, rows in maps.items() for user, value in rows.items()]
            require(plan.version == version and input_state(plan) == result['inputs'], '预检期间输入发生变化', 'stale')
        plan.status = 'blocked' if result['issues'] else 'ready'
        plan.preview_json = encode(result)
        plan.preview_hash = digest(result) if not result['issues'] else ''
        atomic_json(folder / 'preview.json', {'说明': '只读预检结果，尚未创建目标仓库', '预检': result})
    except Exception as exc:
        db.session.rollback()
        plan = get(SvnMigrationPlan, plan_id)
        plan.status, plan.preview_hash = 'blocked', ''
        plan.error = str(exc) if isinstance(exc, MigrationError) else '预检内部错误：' + type(exc).__name__
        result['issues'].append({'code': getattr(exc, 'code', 'internal'), 'message': plan.error})
        plan.preview_json = encode(result)
    finally:
        if api:
            api.close()
        finish(plan, checked, result, plan.error)
        db.session.commit()


def checkpoint(item, **values):
    state = json.loads(item.checkpoint_json)
    state.update(values)
    item.checkpoint_json = encode(state)
    db.session.commit()
    return state


def ensure_group(api, spec, run, item):
    if spec.get('target_group'):
        selected = api.verify_selected_scope(spec['target_group'])
        require(selected['id'] == spec['group_id'] and selected['path'] == spec['owner'],
                '目标群组与执行快照不一致', 'target_group_changed', 409)
        checkpoint(item, group_id=selected['id'])
        return selected['id']
    state = json.loads(item.checkpoint_json)
    group_id = state.get('group_id') or spec['group_id']
    existing = api.api('GET', '/orgs/' + spec['owner'], missing=True)
    if not group_id:
        binding = SvnTargetBinding.query.filter_by(server_id=run.server_id, source_id=spec['source_id'], group_path=spec['owner']).first()
        if binding:
            group_id = binding.group_id
        elif existing:
            remote = api.api('GET', '/governance/groups/' + str(existing['id']))
            # 回执丢失只接管不可猜测的本预览标记，绝不按名称接管普通群组。
            require(state.get('group_intent') and remote.get('name') == spec['group_name'], '目标群组已经存在但不能确认属于此任务', 'uncertain', 409)
            group_id = remote['id']
        else:
            checkpoint(item, group_intent=True)
            created = api.api('POST', '/governance/groups', {'path': spec['owner'], 'name': spec['group_name'], 'parent_id': 0, 'visibility': 2})
            group_id = created['id']
        checkpoint(item, group_id=group_id)
        if not binding:
            db.session.add(SvnTargetBinding(source_id=spec['source_id'], server_id=run.server_id, group_id=group_id,
                                           group_path=spec['owner'], actor_id=api.actor['id']))
            db.session.commit()
    api.group_private(group_id, spec['owner'], api.actor['id'])
    checkpoint(item, group_id=group_id)
    return group_id


def verify_source(source, spec):
    source.unchanged(spec)
    fresh = source.inventory(spec['url'], spec['layout'])
    require(fresh['fingerprint'] == spec['fingerprint'], '来源内容、日志或属性已变化，请重新规划', 'source_changed', 409)


def migrate_item(run, item, spec, api, credential_dir):
    if spec.get('target_group'):
        api.verify_selected_scope(spec['target_group'])
    folder = work_root() / ('run-' + str(run.id)) / ('item-' + str(item.id))
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_data = source_runtime(spec['source_id'], spec['source_version'])
    require(get(SvnRepository, spec['repository_id']).version == spec['repository_version'], '仓库配置已变化', 'stale', 409)
    process = Process(folder, lambda: check_tick(run.id), lambda text: event(run, item, item.stage, text), (source_data['password'], api.token))
    api.process = process
    source = Source(source_data, process, credential_dir)
    stage = lambda key, text: event(run, item, key, text)
    stage('source_check', '正在核对来源身份、固定修订及历史元数据')
    verify_source(source, spec)
    state = json.loads(item.checkpoint_json)
    if item.status == 'success':
        repo = api.repository(spec['owner'], spec['name'])
        require(repo['id'] == state.get('repository_id'), '已完成仓库被删除或替换', 'uncertain', 409)
        require(api.refs(spec['owner'], spec['name']) == state.get('refs'), '已完成仓库引用已经改变，不会覆盖', 'uncertain', 409)
        api.repository_private(repo, spec['owner'], spec['name'], state['group_id'], api.actor['id'], spec.get('target_group'))
        stage('verified', '已完成仓库回读一致，跳过重复迁移')
        return
    item.status = 'running'; db.session.commit()
    if state.get('refs'):
        require((folder / 'converted' / '.git').is_dir(), '转换检查点缺失，不能自动恢复远端写入', 'uncertain', 409)
        for ref, sha in state['refs'].items():
            actual = process.run(['git', 'rev-parse', ref], cwd=folder / 'converted').decode().strip()
            require(actual == sha, '本地转换检查点被修改', 'uncertain', 409)
        expected = state['refs']
    else:
        stage('converting', '正在转换 SVN 文件与历史')
        expected = convert(source, spec, spec['authors_map'], folder, lambda text: stage('converting', text))
        checkpoint(item, refs=expected)
    stage('verifying_history', '正在逐提交验证源内容、作者、时间及日志')
    verification = verify_history(source, spec, spec['authors_map'], folder, expected, lambda text: stage('verifying_history', text))
    checkpoint(item, history_verified=True)
    verify_source(source, spec)
    stage('creating', '正在确认目标群组和私有仓库')
    group_id = ensure_group(api, spec, run, item)
    state = json.loads(item.checkpoint_json)
    repository = api.repository(spec['owner'], spec['name'], missing=True)
    marker = 'Gitea Manager SVN 迁移 ' + digest([run.id, spec['repository_id'], json.loads(run.snapshot_json)['preflight_hash']])
    if repository:
        if not state.get('repository_id'):
            require(state.get('repository_intent') and repository.get('description') == marker, '已存在目标不能确认为本任务创建', 'uncertain', 409)
            checkpoint(item, repository_id=repository['id'])
        else:
            require(state['repository_id'] == repository['id'], '目标仓库 ID 已改变', 'uncertain', 409)
    else:
        require(not state.get('repository_id'), '本任务创建的目标仓库已丢失', 'uncertain', 409)
        checkpoint(item, repository_intent=True)
        repository = api.api('POST', '/orgs/' + spec['owner'] + '/repos', {'name': spec['name'], 'private': True,
            'auto_init': False, 'default_branch': spec['layout']['default_branch'], 'description': marker})
        checkpoint(item, repository_id=repository['id'])
    api.repository_private(repository, spec['owner'], spec['name'], group_id, api.actor['id'], spec.get('target_group'))
    # 导入旧源码不能顺带运行其中的工作流；迁移完成后由管理员按需启用。
    if repository.get('has_actions') is not False:
        api.api('PATCH', '/repos/' + spec['owner'] + '/' + spec['name'], {'has_actions': False})
        repository = api.repository(spec['owner'], spec['name'])
    require(repository.get('has_actions') is False, '目标 Actions 未关闭，停止推送以避免导入触发自动执行', 'configuration')
    actual = api.refs(spec['owner'], spec['name'])
    require(actual == expected or not actual, '目标已有不同引用，已停止且不会强制覆盖', 'uncertain', 409)
    if not actual:
        stage('pushing', '正在原子推送全部分支和标签')
        checkpoint(item, push_intent=True)
        try:
            process.run(['git', 'push', '--atomic', api.remote(spec['owner'], spec['name']), *[ref + ':' + ref for ref in expected]],
                        cwd=folder / 'converted', env=api.git_env())
        except Cancelled:
            raise
        except MigrationError:
            if api.refs(spec['owner'], spec['name']) != expected:
                raise Uncertain('推送未获确认，远端结果待核对') from None
    require(api.refs(spec['owner'], spec['name']) == expected, '推送后的远端引用不一致', 'uncertain', 409)
    if repository.get('default_branch') != spec['layout']['default_branch']:
        api.api('PATCH', '/repos/' + spec['owner'] + '/' + spec['name'], {'default_branch': spec['layout']['default_branch']})
    stage('verifying_remote', '正在从 Gitea 重新克隆并核对全部引用和对象')
    clone = folder / 'verified.git'
    if clone.exists():
        shutil.rmtree(clone)
    process.run(['git', 'clone', '--mirror', api.remote(spec['owner'], spec['name']), str(clone)], env=api.git_env())
    process.run(['git', 'fsck', '--full'], cwd=clone)
    refs = process.run(['git', 'for-each-ref', '--format=%(refname) %(objectname)'], cwd=clone).decode().splitlines()
    require({row.split()[0]: row.split()[1] for row in refs} == expected, '重新克隆的引用不一致', 'verification')
    count = int(process.run(['git', 'rev-list', '--all', '--count'], cwd=clone))
    require(count == verification['提交数'], '重新克隆的提交数不一致', 'verification')
    verify_source(source, spec)
    repository = api.repository(spec['owner'], spec['name'])
    require(repository['id'] == json.loads(item.checkpoint_json)['repository_id'], '最终目标 ID 改变', 'uncertain', 409)
    api.repository_private(repository, spec['owner'], spec['name'], group_id, api.actor['id'], spec.get('target_group'))
    require(repository.get('has_actions') is False, '迁移期间目标 Actions 配置发生变化，请核对', 'uncertain', 409)
    require(api.refs(spec['owner'], spec['name']) == expected, '最终引用发生外部变化', 'uncertain', 409)
    item.status = 'success'
    item.result_json = encode({'来源': spec['url'], 'UUID': spec['uuid'], '固定修订': spec['revision'],
        '目标': api.base + '/' + spec['owner'] + '/' + spec['name'], '仓库ID': repository['id'], '引用': expected,
        '提交数': count, '已验证提交数': verification['已验证提交数'], '属性': spec['properties'],
        '空目录': spec['empty_directories'],
        '提交映射': verification['映射'],
        '修订覆盖': verification['修订覆盖'],
        '作者': spec['authors_map'], '验证': '逐提交内容与元数据、重新克隆、引用和对象完整性均通过',
        '边界': '业务账号与权限未迁移；空目录和特殊 SVN 属性仅保留报告；合并结果保留但不重建完整合并图。'})
    stage('completed', '迁移及全量内容验证完成')


def execute(run_id):
    run = get(SvnMigrationRun, run_id)
    current_item, api = None, None
    try:
        with target_guard(run.server_id, migration=True):
            with file_lock(work_root() / 'enqueue.lock', blocking=True):
                claimed = db.session.execute(db.update(SvnMigrationRun).where(
                    SvnMigrationRun.id == run.id, SvnMigrationRun.status == 'queued').values(status='running', error=''))
                db.session.commit()
                if claimed.rowcount != 1:
                    return
                db.session.refresh(run)
            from access.models import ManagerAccount
            account = db.session.get(ManagerAccount, run.actor_id)
            require(account and account.enabled and account.is_platform_admin, '发起人已停用或不再拥有平台管理权限', 'authorization')
            server = get(GiteaServer, run.server_id)
            snapshot = json.loads(run.snapshot_json)
            require(target_fingerprint(server) == snapshot['inputs']['target'], '目标连接身份或令牌已改变，请重新规划', 'stale')
            folder = work_root() / ('run-' + str(run.id))
            folder.mkdir(mode=0o700, exist_ok=True)
            with __import__('tempfile').TemporaryDirectory(dir=folder, prefix='credentials-') as secret_dir:
                process = Process(folder, lambda: check_tick(run.id))
                api = Gitea(server, process, secret_dir, snapshot['target_ca_pem'])
                require(api.check() == snapshot['actor'], 'Gitea 迁移管理员身份已改变', 'stale')
                items = SvnMigrationItem.query.filter_by(run_id=run.id).order_by(SvnMigrationItem.position).all()
                for current_item, spec in zip(items, snapshot['repositories']):
                    check_tick(run.id)
                    db.session.refresh(run)
                    if run.control == 'pause':
                        run.status, run.control = 'paused', ''; db.session.commit()
                        return
                    migrate_item(run, current_item, spec, api, secret_dir)
                # 跨来源批次结束时再次核对早先完成的来源，避免后续迁移期间恢复写入。
                for completed_item, spec in zip(items, snapshot['repositories']):
                    source_data = source_runtime(spec['source_id'], spec['source_version'])
                    verify_source(Source(source_data, api.process, secret_dir), spec)
                    remote = api.repository(spec['owner'], spec['name'])
                    saved = json.loads(completed_item.checkpoint_json)
                    require(remote['id'] == saved['repository_id'] and api.refs(spec['owner'], spec['name']) == saved['refs'],
                            '批次结束时目标引用或身份已变化', 'uncertain', 409)
                    api.repository_private(remote, spec['owner'], spec['name'], saved['group_id'], api.actor['id'], spec.get('target_group'))
                run.status = 'success'
                release_reservations(run)
                atomic_json(folder / 'report.json', {'说明': '迁移结果，业务账号与权限留待后续配置', '结果': public_run(run, True, True)})
                event(run, None, 'completed', '全部仓库迁移成功，初始访问保持私有')
    except Exception as exc:
        db.session.rollback()
        run = get(SvnMigrationRun, run_id)
        if isinstance(exc, Cancelled):
            run.status = 'cancelled'
        elif isinstance(exc, Uncertain) or getattr(exc, 'code', '') == 'uncertain':
            run.status = 'uncertain'
        elif getattr(exc, 'code', '') in ('busy', 'maintenance'):
            run.status = 'queued'
        else:
            run.status = 'partial' if SvnMigrationItem.query.filter_by(run_id=run.id, status='success').count() else 'failed'
        run.error = str(exc) if isinstance(exc, MigrationError) else '迁移内部错误：' + type(exc).__name__
        if current_item and current_item.status != 'success':
            current_item.status = 'uncertain' if run.status == 'uncertain' else 'failed'
        if run.status in ('cancelled', 'failed', 'partial'):
            release_reservations(run)
        run.control = ''
        event(run, current_item, 'interrupted', run.error)
    finally:
        if api:
            api.close()
        if current_item:
            strip_checkout(work_root() / ('run-' + str(run.id)) / ('item-' + str(current_item.id)) / 'converted')
        db.session.commit()


def step():
    from services.upgrade_maintenance import active
    if active() or STOP:
        return False
    heartbeat()
    from .discovery import step as discovery_step
    if discovery_step(check_tick):
        return True
    source = SvnSource.query.filter(SvnSource.test_json == '{"status":"queued"}').order_by(SvnSource.id).first()
    if source:
        folder = work_root() / ('probe-' + str(source.id))
        folder.mkdir(mode=0o700, exist_ok=True)
        try:
            with __import__('tempfile').TemporaryDirectory(dir=folder, prefix='credentials-') as secret_dir:
                value = source_runtime(source.id)
                process = Process(folder, check_tick, secrets=(value['password'],))
                repository = SvnRepository.query.filter_by(source_id=source.id, archived=False).order_by(SvnRepository.id).first()
                require(repository is not None, '请先登记至少一个仓库 URL，再检测来源连接')
                result = Source(value, process, secret_dir).info(repository.url)
                source.test_json = encode({'status': 'success', 'message': '连接成功；完整读取范围仍须由来源管理员确认', 'info': result})
        except MigrationError as exc:
            source.test_json = encode({'status': 'failed', 'message': str(exc)})
        source.tested_at = datetime.utcnow(); db.session.commit()
        return True
    run = SvnMigrationRun.query.filter_by(status='queued').order_by(SvnMigrationRun.id).first()
    if run:
        execute(run.id)
        return True
    plan = SvnMigrationPlan.query.filter_by(status='queued').order_by(SvnMigrationPlan.id).first()
    if plan:
        preflight(plan.id)
        return True
    from .layout_detection import step as detect_layout_step
    return detect_layout_step(check_tick)


def create_worker_app():
    from config import DATABASE_URL
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI=DATABASE_URL, SQLALCHEMY_TRACK_MODIFICATIONS=False,
                      SQLALCHEMY_ENGINE_OPTIONS={'connect_args': {'timeout': 30}})
    db.init_app(app)
    from access import models as access_models
    from services.database_startup import database_startup_lock
    with app.app_context(), database_startup_lock(app):
        db.create_all()
    return app


def main():
    global STOP
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Gitea Manager SVN 串行迁移执行器')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--check', action='store_true', help='检查工具链和唯一执行器锁，不写数据库')
    args = parser.parse_args()
    if args.check:
        import subprocess
        subprocess.run(['git', 'svn', '--version'], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['svn', '--version', '--quiet'], check=True, stdout=subprocess.DEVNULL)
        app = Flask(__name__)
        from config import DATABASE_URL
        app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
        db.init_app(app)
        with app.app_context():
            try:
                with file_lock(work_root() / 'worker.lock'):
                    raise SystemExit('唯一迁移执行器未运行')
            except MigrationError as exc:
                if exc.code != 'busy':
                    raise
        print('迁移工具链和执行器锁检查通过')
        return
    def stop(*_):
        global STOP
        STOP = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    app = create_worker_app()
    with app.app_context(), file_lock(work_root() / 'worker.lock'):
        recovered = False
        while not STOP:
            from services.upgrade_maintenance import active
            if not recovered and not active():
                reap_processes(work_root())
                # 唯一执行器在维护解除后恢复，升级核对期间不改写旧记录。
                for row in SvnMigrationRun.query.filter_by(status='running'):
                    row.status, row.error = 'uncertain', '执行器中断，请核对检查点后恢复'
                for row in SvnMigrationPlan.query.filter_by(status='preflight'):
                    row.status = 'queued'
                    from .models import SvnPreflight
                    # 中断的预检继续使用同一记录，不留下永久“预检中”的历史。
                    SvnPreflight.query.filter_by(plan_id=row.id, status='preflight').update({'status': 'queued'})
                from .models import SvnLayoutDetection, SvnDiscovery
                SvnLayoutDetection.query.filter_by(status='running').update({'status': 'queued'})
                SvnDiscovery.query.filter_by(status='running').update({'status': 'queued'})
                db.session.commit()
                recovered = True
            step()
            db.session.remove()
            if args.once:
                break
            time.sleep(1)


if __name__ == '__main__':
    main()
