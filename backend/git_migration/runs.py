"""固定执行计划、目标互斥与隔离写入检查点。"""
from datetime import datetime
import json
import secrets

from models import db, GiteaServer
from svn_migration.common import digest, encode, file_lock, require, MigrationError
from svn_migration.engine import Process
from svn_migration.guards import target_guard
from .models import GitMigrationRun, GitMigrationSource, GitMigrationInventory
from .store import root, get, busy
from .target import Receiver
from .normalize import objects
from .inventory import Inventory
from .assets import plan_assets, transfer_assets
from .ci import ci_assets
from .git_snapshot import GitSnapshot
from .discussions import historical_diff
from .packages import package_assets, verification_plan
from . import rules

ACTIVE = ('queued', 'running', 'interrupted', 'uncertain', 'quarantined')


def public(row, detail=True):
    result = {'id': row.id, 'source_id': row.source_id, 'inventory_id': row.inventory_id,
            'server_id': row.server_id, 'status': row.status, 'stage': row.stage,
            'error': row.error,
            'created_at': row.created_at.isoformat() + 'Z',
            'completed_at': row.completed_at.isoformat() + 'Z' if row.completed_at else None}
    if detail:
        result['plan'] = json.loads(row.plan_json)
        fields = {'repo_id', 'repo_name', 'final_path', 'code_verified', 'objects_verified', 'handed_off', 'pending', 'gaps'}
        result['checkpoint'] = {key: {name: value for name, value in item.items() if name in fields}
                                for key, item in json.loads(row.checkpoint_json).items()
                                if key.isdigit() and isinstance(item, dict)}
    return result


def content_digest(value):
    # 下载计数会因验收读取自行变化，不属于需要停写的业务内容。
    if isinstance(value, dict):
        derived = {'download_count', 'last_downloaded_at'}
        if 'id' in value and ('login' in value or 'username' in value):
            derived.add('last_login')  # 读取旧版网页制品会刷新登录时间，不能据此判为来源业务写入。
        if 'source_project_id' in value and 'target_project_id' in value:
            # GitLab 异步重新计算的可合并性不是业务写入；提交、正文和审批仍逐项比较。
            derived.update(('merge_status', 'detailed_merge_status'))
        return {key: content_digest(item) for key, item in value.items() if key not in derived and not (key == 'sha256' and 'rows' in value and 'category' in value)}
    if isinstance(value, list):
        return [content_digest(item) for item in value]
    return value


def target_identity(server):
    # 凭据不进入计划、日志或页面；轮换令牌不改变目标身份。
    return {'id': server.id, 'url': server.gitea_url.rstrip('/'), 'instance_identity': server.instance_identity, 'access_epoch': server.access_epoch}


def enqueue(data, actor_id):
    fields = {'inventory_id', 'server_id', 'group_id', 'request_key', 'source_frozen'}
    require(fields <= set(data) <= fields | {'principal_map'}, '执行请求字段不完整或不支持')
    require(data['source_frozen'] is True, '正式迁移需要确认来源已经停止写入')
    for name in ('inventory_id', 'server_id', 'group_id'):
        require(type(data[name]) is int and data[name] > 0, '执行请求编号无效')
    import re
    require(isinstance(data['request_key'], str) and re.fullmatch(r'[A-Za-z0-9_-]{16,80}', data['request_key']), '请求标识无效')
    with file_lock(root() / 'enqueue.lock', blocking=True):
        existing = GitMigrationRun.query.filter_by(request_key=data['request_key']).first()
        if existing:
            require(json.loads(existing.plan_json)['request'] == data and existing.actor_id == actor_id,
                    '请求标识已用于其他输入', 'conflict', 409)
            return existing
        inventory = get(GitMigrationInventory, data['inventory_id'])
        source = get(GitMigrationSource, inventory.source_id)
        require(inventory.status == 'succeeded' and inventory.source_version == source.version and not source.archived,
                '盘点结果已失效，请重新盘点', 'stale', 409)
        require(not busy(source.id), '来源已有执行任务', 'busy', 409)
        server = get(GiteaServer, data['server_id'])
        with target_guard(server.id):
            require(not GitMigrationRun.query.filter(GitMigrationRun.server_id == server.id,
                    GitMigrationRun.status.in_(ACTIVE)).first(), '目标已有尚未交接的迁移', 'busy', 409)
            folder = root() / 'planning' / secrets.token_hex(16)
            folder.mkdir(parents=True, mode=0o700)
            receiver = Receiver(server, Process(folder / 'commands'), folder)
            try:
                receiver.check()
                selection = receiver.selected_scope(data['group_id'])
                snapshot = json.loads(inventory.result_json)
                mapping = rules.validate_mapping(data.get('principal_map', {}), rules.requirements(snapshot), receiver)
            finally:
                receiver.close()
            snapshot = json.loads(inventory.result_json)
            require(snapshot.get('projects'), '盘点没有项目')
            plan = {'request': data, 'target': target_identity(server), 'selection': selection,
                    'inventory_digest': digest(snapshot), 'key': secrets.token_hex(16), 'principal_map': mapping}
            row = GitMigrationRun(source_id=source.id, source_version=source.version, inventory_id=inventory.id,
                                  server_id=server.id, actor_id=actor_id, request_key=data['request_key'], plan_json=encode(plan))
            db.session.add(row)
            db.session.commit()
            return row


def resume(ident):
    with file_lock(root() / 'enqueue.lock', blocking=True):
        row = get(GitMigrationRun, ident)
        require(row.status in ('interrupted', 'uncertain', 'quarantined'), '当前状态不能继续执行', 'conflict', 409)
        source = get(GitMigrationSource, row.source_id)
        require(source.version == row.source_version and not source.archived, '来源配置已经变化', 'stale', 409)
        row.status, row.error, row.completed_at = 'queued', '', None
        db.session.commit()
        return row


def execute(row, provider, tick):
    plan = json.loads(row.plan_json)
    inventory = get(GitMigrationInventory, row.inventory_id)
    frozen = json.loads(inventory.result_json)
    require(digest(frozen) == plan['inventory_digest'], '固定盘点内容已变化', 'stale')
    server = get(GiteaServer, row.server_id)
    require(target_identity(server) == plan['target'], '目标连接地址已变化', 'stale')
    folder = root() / 'runs' / str(row.id)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    checkpoint = json.loads(row.checkpoint_json)
    checkpoint.setdefault('key', plan['key'])

    def save():
        tick()
        row.checkpoint_json = encode(checkpoint)
        db.session.commit()

    with target_guard(server.id, migration=True, git_run_id=row.id):
        receiver = Receiver(server, Process(folder / 'commands', tick=tick), folder)
        try:
            receiver.check()
            receiver.verify_selected_scope(plan['selection'])
            identity = provider.authenticate()
            require(identity['actor'] == frozen['actor'] and identity['version'] == frozen['version'], '来源身份或版本已变化', 'stale')
            # 所有依赖仓库先固定并建立代码，再导入跨仓库关系；仓库编号跨转移保持不变。
            for item in frozen['projects']:
                project = item['project']
                expected = {k: v for k, v in item.items() if k not in ('code_snapshot', 'dependency')}
                require(digest(content_digest(Inventory(provider).inspect(project))) == digest(content_digest(expected)),
                        '来源元数据已改变，不能沿用固定依赖图', 'source_changed')
                progress = checkpoint.setdefault(str(project['id']), {'key': secrets.token_hex(16)})
                if progress.get('sealed') or progress.get('handed_off'):
                    continue
                row.stage = '固定依赖仓库与代码：' + project['path']
                save()
                if 'snapshot' not in progress:
                    code_dir = folder / (str(project['id']) + '-' + secrets.token_hex(8))
                    progress['snapshot'] = GitSnapshot(provider, project, code_dir, tick=tick).capture()
                    progress['directory'] = str(code_dir)
                    save()
                if item.get('code_snapshot'):
                    require(progress['snapshot']['refs'] == item['code_snapshot']['refs'] and
                            progress['snapshot']['lfs_objects'] == item['code_snapshot']['lfs_objects'],
                            '依赖代码已偏离固定盘点', 'source_changed')
                repo = receiver.stage(progress['key'], digest([progress['snapshot'], expected]), progress,
                                      lambda _: save(), batch_key=checkpoint['key'])
                if not progress.get('code_transferred'):
                    receiver.transfer_code(progress['snapshot'], progress['directory'], repo, progress, lambda _: save())
            repository_map = {item['project']['id']: checkpoint[str(item['project']['id'])] for item in frozen['projects']}
            for item in frozen['projects']:
                project = item['project']
                current = Inventory(provider).inspect(project)
                expected = {k: v for k, v in item.items() if k not in ('code_snapshot', 'dependency')}
                require(digest(content_digest(current)) == digest(content_digest(expected)), '来源元数据已经变化，请保持停写并重新盘点', 'source_changed')
                progress = checkpoint.setdefault(str(project['id']), {'key': secrets.token_hex(16)})
                if progress.get('sealed') or progress.get('handed_off'):
                    continue
                row.stage = '迁移 ' + project['path']
                save()
                def stage(label):
                    row.stage = label + '：' + project['path']
                    save()
                if 'snapshot' not in progress:
                    stage('固定来源代码与 LFS')
                    # 每次新尝试保留先前失败现场，绝不覆盖半成品。
                    code_dir = folder / (str(project['id']) + '-' + secrets.token_hex(8))
                    progress['snapshot'] = GitSnapshot(provider, project, code_dir, tick=tick).capture()
                    progress['directory'] = str(code_dir)
                    save()
                snapshot = progress['snapshot']
                if item.get('code_snapshot'):
                    require(snapshot['refs'] == item['code_snapshot']['refs'] and snapshot['lfs_objects'] == item['code_snapshot']['lfs_objects'], '来源代码已偏离盘点', 'source_changed')
                def resolve_tag(tag):
                    return receiver.process.run(['git', 'rev-parse', '--verify', 'refs/tags/' + tag + '^{commit}'], cwd=folder / progress['directory'] / 'repository.git').decode().strip()
                normalized = objects(item, provider.kind, resolve_tag=resolve_tag, repository_map=repository_map,
                    resolve_diff=lambda position: historical_diff(receiver.process,
                        folder / progress['directory'] / 'repository.git', position))
                assets = plan_assets(item, provider.kind, normalized, progress['key'], provider.http.base, receiver.base)
                assets.extend(package_assets(item, provider.kind, normalized, provider.http.base))
                progress['package_files'] = verification_plan(normalized, assets)
                progress['rule_plan'] = rules.convert(item, provider.kind, plan.get('principal_map', {}), normalized)
                progress['principal_map'] = plan.get('principal_map', {})
                historical_files, historical_gaps = ci_assets(item, provider.kind, provider.http.base)
                assets.extend(historical_files)
                normalized['pending'].extend(historical_gaps)
                stage('创建并核对隔离仓库')
                repo = receiver.stage(progress['key'], digest([snapshot, expected]), progress, lambda _: save(), batch_key=checkpoint['key'])
                stage('传输代码、引用与 LFS')
                receiver.transfer_code(snapshot, progress['directory'], repo, progress, lambda _: save(),
                                       [obj for obj in normalized['objects'] if obj['kind'] == 'pull'])
                stage('导入并回读历史协作资料')
                receiver.import_objects(progress['key'], normalized['objects'], progress, lambda _: save())
                wiki = next((d for d in item['datasets'] if d['category'] == 'Wiki 页面'), None)
                if wiki is not None:
                    stage('传输并核验 Wiki 历史')
                    try:
                        receiver.transfer_wiki(provider, project, progress['key'], repo, folder / ('wiki-' + str(project['id'])), progress, lambda _: save())
                        normalized['pending'] = [p for p in normalized['pending'] if p['category'] != 'Wiki 页面']
                    except MigrationError as error:
                        if error.code != 'git_read' or wiki['rows']:
                            raise
                        normalized['pending'].append({'category': 'Wiki 历史', 'reason': '当前页面为空且 Wiki Git 仓库不可读，不能排除已删除页面的历史'})
                stage('传输并核验附件、制品与日志')
                transfer_assets(provider, receiver, progress['key'], assets, folder / ('files-' + secrets.token_hex(8)), progress, lambda _: save())
                stage('重新克隆目标并核对来源停写')
                receiver.verify_code(snapshot, repo, folder / ('verify-' + secrets.token_hex(8)), progress, lambda _: save())
                require(digest(content_digest(Inventory(provider).inspect(project))) == digest(content_digest(expected)), '写入期间来源元数据改变，目标保留隔离', 'source_changed')
                progress['pending'], progress['gaps'] = normalized['pending'], normalized['gaps']
                save()
            receiver.verify_selected_scope(plan['selection'])
            ready = all(not checkpoint[str(item['project']['id'])].get('pending') and not checkpoint[str(item['project']['id'])].get('gaps') for item in frozen['projects'])
            if ready:
                from .handoff import finish
                for item in frozen['projects']:
                    progress = checkpoint[str(item['project']['id'])]
                    if not progress.get('handed_off'):
                        GitSnapshot(provider, item['project'], folder / ('source-final-' + secrets.token_hex(8)), tick=tick).verify_refs(progress['snapshot'])
                        if progress.get('wiki_snapshot'):
                            GitSnapshot(provider, item['project'], folder / ('wiki-final-' + secrets.token_hex(8)), tick=tick, wiki=True).verify_refs(progress['wiki_snapshot'])
                        row.stage = '封存、规则核对与最终交接：' + item['project']['path']
                        save()
                        finish(receiver, plan['selection'], item['project'], progress, checkpoint, save)
                row.status, row.stage = 'succeeded', '迁移核验与群组交接完成'
            else:
                row.status, row.stage = 'quarantined', '隔离写入完成，等待完整性与规则交接'
            row.completed_at = datetime.utcnow()
            save()
        finally:
            receiver.close()
