"""独立预迁移、阻断清单与正式启动前复检，不承诺未来故障不会发生。"""
from datetime import datetime, timedelta
import json
import secrets
from pathlib import Path

from models import db, GiteaServer
from svn_migration.common import require, encode, digest, file_lock, MigrationError
from svn_migration.engine import Process
from svn_migration.guards import target_guard
from .models import GitMigrationPreflight, GitMigrationSource, GitMigrationInventory, GitMigrationAccountMapping
from .store import get, root, busy
from .target import Receiver
from .git_snapshot import GitSnapshot
from .inventory import Inventory
from .source_manifest import capture, verify_plan
from . import runs, rules

ACTIVE = ('queued', 'running', 'ready', 'blocked', 'interrupted')


def stale(row, *, check_expiry=True):
    source = db.session.get(GitMigrationSource, row.source_id)
    inventory = db.session.get(GitMigrationInventory, row.inventory_id)
    server = db.session.get(GiteaServer, row.server_id)
    plan = json.loads(row.plan_json)
    if plan.get('preflight_protocol') != 8:
        return '预迁移协议已升级，请结束旧预迁移并重新检查'
    if not source or source.archived or source.version != row.source_version or not inventory or inventory.status != 'succeeded' or not server:
        return '来源、盘点或目标连接已变化'
    if digest(json.loads(inventory.result_json)) != plan['inventory_digest'] or runs.target_identity(server) != plan['target']:
        return '盘点内容或目标实例身份已变化'
    receipt = plan.get('mapping_receipt') or {}
    if receipt:
        mapping = db.session.get(GitMigrationAccountMapping, receipt['mapping_id'])
        if not mapping or mapping.revision != receipt['revision']:
            return '人员处理方式已变化，必须重新预迁移'
    if check_expiry and row.expires_at and row.expires_at <= datetime.utcnow():
        return '预迁移结果已超过三十分钟有效期'
    return ''


def public(row, detail=True):
    result = json.loads(row.result_json)
    checks = result.get('checks', [])
    reason = stale(row) if row.status == 'ready' else ''
    value = {'id': row.id, 'source_id': row.source_id, 'inventory_id': row.inventory_id, 'server_id': row.server_id,
        'status': row.status, 'phase': row.phase, 'stage': row.stage, 'error': row.error, 'run_id': row.run_id,
        'stale': reason, 'can_start': row.status == 'ready' and not reason and result.get('rehearsal') == 'passed' and bool(checks) and all(c['status'] == 'passed' for c in checks),
        'checks_passed': sum(c['status'] == 'passed' for c in checks), 'checks_total': len(checks),
        'phase': row.phase, 'created_at': row.created_at.isoformat()+'Z', 'expires_at': row.expires_at.isoformat()+'Z' if row.expires_at else None}
    if detail:
        value['result'] = result
        value['capacity'] = json.loads(row.checkpoint_json).get('capacity')
        plan = json.loads(row.plan_json)
        value['mapping_id'] = plan.get('mapping_receipt', {}).get('mapping_id')
        value['target'] = plan['selection']
        from .discussion_history import compatibility
        inventory = get(GitMigrationInventory, row.inventory_id)
        value['compatibility'] = compatibility(plan, json.loads(inventory.result_json))
    return value


def enqueue(data, actor_id):
    require(isinstance(data, dict) and {'inventory_id','server_id','group_id','request_key'} <= set(data)
            and set(data) <= {'inventory_id','server_id','group_id','request_key','mapping_id'}, '预迁移请求字段无效')
    old = GitMigrationPreflight.query.filter_by(request_key=data['request_key']).first()
    if old:
        require(old.actor_id == actor_id and json.loads(old.plan_json)['request'] == {**data,'source_frozen':False}, '请求标识已用于其他预迁移', 'conflict', 409)
        return old
    # 共用正式规划、人员、身份及实例锁检查；不会创建 GitMigrationRun。
    source, inventory, server, plan = runs.enqueue({**data, 'source_frozen':False}, actor_id, planning_only=True)
    require(all(p.get('source_manifest', {}).get('schema') == 3 for p in json.loads(inventory.result_json)['projects']),
            '讨论完整性校验已升级，请重新盘点来源', 'inventory_required', 409)
    plan['preflight_protocol']=8
    require(inventory.capture_code, '预迁移需要带代码快照的完整盘点，请重新盘点', 'snapshot_required', 409)
    with file_lock(root()/'enqueue.lock', blocking=True):
        old = GitMigrationPreflight.query.filter_by(request_key=data['request_key']).first()
        if old:
            require(old.actor_id == actor_id and json.loads(old.plan_json)['request'] == {**data,'source_frozen':False}, '请求标识已用于其他预迁移', 'conflict', 409)
            return old
        require(not busy(source.id), '来源已有运行中的任务或预迁移', 'busy', 409)
        require(not GitMigrationPreflight.query.filter_by(source_id=source.id).filter(GitMigrationPreflight.status.in_(ACTIVE)).first(), '来源已有预迁移，请先处理或结束', 'busy',409)
        with target_guard(server.id):
            row = GitMigrationPreflight(source_id=source.id, source_version=source.version, inventory_id=inventory.id,
                server_id=server.id, actor_id=actor_id, request_key=data['request_key'], plan_json=encode(plan),
                result_json=encode({'checks':[], 'notice':'检查覆盖率不是成功概率；正式启动前会重新检查来源与配置。'}))
            previous=GitMigrationPreflight.query.filter_by(source_id=source.id,server_id=server.id,status='cancelled',actor_id=actor_id).order_by(GitMigrationPreflight.id.desc()).first()
            if previous and json.loads(previous.plan_json)['selection']['id']==plan['selection']['id']:
                old=json.loads(previous.checkpoint_json)
                if old.get('groups'):row.checkpoint_json=encode({'groups':old['groups']})
            db.session.add(row); db.session.commit()
            return row


def request_start(ident, data, actor_id):
    require(isinstance(data,dict) and set(data)=={'source_frozen','request_key'} and data['source_frozen'] is True,
            '正式启动前必须确认全部来源及依赖已经停止写入')
    import re
    require(isinstance(data['request_key'],str) and re.fullmatch(r'[A-Za-z0-9_-]{16,80}',data['request_key']), '启动请求标识无效')
    with file_lock(root()/'enqueue.lock',blocking=True):
        row=get(GitMigrationPreflight,ident)
        plan=json.loads(row.plan_json)
        if row.run_id or row.phase=='activate':
            require(plan.get('activation')==data and row.actor_id==actor_id, '已经提交过其他启动请求','conflict',409)
            return row
        report=json.loads(row.result_json)
        from .discussion_history import compatibility
        require(compatibility(plan, json.loads(get(GitMigrationInventory, row.inventory_id).result_json))['acknowledged'],
                '请先确认来源与目标的讨论规则差异', 'compatibility_required', 409)
        require(row.status=='ready' and json.loads(row.checkpoint_json).get('batch_prepared') and report.get('rehearsal')=='passed' and report.get('checks')
                and all(c['status']=='passed' for c in report['checks']) and not stale(row),
                stale(row) or '预迁移与全部检查必须通过后才能正式启动','preflight_required',409)
        require(row.actor_id==actor_id,'请由预迁移发起管理员确认启动','authorization',403)
        plan['activation']=data
        row.plan_json=encode(plan); row.phase='activate'; row.status='queued'; row.stage='等待正式启动前复检'
        db.session.commit(); return row


def acknowledge_compatibility(ident, data, actor_id):
    from .discussion_history import compatibility
    require(isinstance(data, dict) and set(data) == {'digest'}, '请提交完整的行为差异摘要')
    with file_lock(root()/'enqueue.lock', blocking=True):
        row = get(GitMigrationPreflight, ident)
        plan = json.loads(row.plan_json)
        require(row.actor_id == actor_id, '请由预迁移发起管理员确认差异', 'authorization', 403)
        require(not stale(row), stale(row), 'stale', 409)
        current = compatibility(plan, json.loads(get(GitMigrationInventory, row.inventory_id).result_json))
        require(current['differences'] and data['digest'] == current['digest'], '行为差异已变化，请刷新后确认', 'stale', 409)
        if current['acknowledgement']:
            return row
        require(row.status == 'blocked' and row.phase == 'trial' and not json.loads(row.checkpoint_json).get('batch_prepared'),
                '当前预迁移不能确认新的行为差异', 'conflict', 409)
        plan['compatibility_acknowledgement'] = {'digest': current['digest'], 'actor_id': actor_id,
                                               'confirmed_at': datetime.utcnow().isoformat()+'Z'}
        row.plan_json = encode(plan)
        row.status, row.stage, row.error, row.error_code = 'queued', '已确认行为差异，等待重新预检', '', ''
        db.session.commit()
        return row


def request_cancel(ident):
    with file_lock(root()/'enqueue.lock',blocking=True):
        row=get(GitMigrationPreflight,ident)
        require(row.status in ('ready','blocked','interrupted'), '运行中或已使用的预迁移不能直接结束','busy',409)
        row.phase='cancel'; row.status='queued'; row.stage='等待封存预迁移现场'
        db.session.commit(); return row


def rotate_target_credentials(ident, token):
    """允许恢复原管理员凭据以安全关闭现场，不解除实例互斥。"""
    from types import SimpleNamespace
    require(isinstance(token,str) and 0 < len(token) <= 4096, '请填写有效目标令牌')
    with file_lock(root()/'enqueue.lock',blocking=True):
        row=get(GitMigrationPreflight,ident)
        require(row.status in ('ready','blocked','interrupted'), '请等待预迁移停止后更新凭据', 'busy',409)
        server=get(GiteaServer,row.server_id);plan=json.loads(row.plan_json)
        require(runs.target_identity(server)==plan['target'], '目标实例身份已变化', 'stale',409)
        folder=root()/'preflights'/str(row.id)/'credential-check'
        with target_guard(server.id,migration=True,git_preflight_id=row.id):
            receiver=Receiver(SimpleNamespace(gitea_url=server.gitea_url,api_token=token),Process(folder),folder)
            try:
                receiver.check()
                require({k:receiver.actor[k] for k in ('id','login')}==plan['target_actor'], '新令牌不属于原目标管理员','identity',409)
            finally:receiver.close()
            server.api_token=token;db.session.commit();return row


def check(row, key, title, operation, remedy, project=''):
    row.stage=title + ('：'+project if project else '')
    db.session.commit()
    result=json.loads(row.result_json)
    try:
        details=operation()
        item={'key':key,'title':title,'project':project,'status':'passed','detail':details if isinstance(details,(str,int,dict,list)) else '通过','remedy':''}
    except MigrationError as error:
        item={'key':key,'title':title,'project':project,'status':'blocked','detail':str(error),'remedy':remedy,'code':error.code}
    result.setdefault('checks',[]).append(item); row.result_json=encode(result); db.session.commit()
    return item['status']=='passed'


def path_check(receiver, selection, projects, owned=None):
    owned=owned or {}
    targets=[selection['path']+'/'+p['project']['path'] for p in projects]
    require(len(set(p.casefold() for p in targets))==len(targets),'目标路径在当前批次中重复','target_conflict')
    for item in projects:
        parts=item['project']['path'].split('/')
        require(len(parts) >= 2 and all(p and p not in ('.', '..') for p in parts), '来源层级路径无效', 'target_conflict')
        group=receiver.api('GET','/governance/groups/'+str(selection['id']))
        for part in parts[:-1]:
            paths=receiver.child_group_paths(group['id'])
            # 当前引擎不会接管他人已有的同名子群组。
            require((group['full_path']+'/'+part).casefold() not in paths or (group['full_path']+'/'+part) in owned.get('groups',{}),
                    '目标已有同名子群组：'+group['full_path']+'/'+part,'target_conflict')
            break
        owner=selection['path']+'/'+ '/'.join(parts[:-1])
        found=receiver.repository(owner,parts[-1],missing=True)
        require(found is None or found['id'] in [v.get('repo_id') for v in owned.values() if isinstance(v,dict)],'目标仓库已经存在：'+owner+'/'+parts[-1],'target_conflict')
    return {'paths':targets}


def verify_source_code(row, provider, project, snapshot, tick):
    require(snapshot, '缺少代码快照', 'snapshot_required')
    # 快照捕获禁止覆盖现场；每一轮试迁移和正式复检使用独立目录。
    directory=root()/'preflights'/str(row.id)/('check-'+str(project['id'])+'-'+secrets.token_hex(8))
    GitSnapshot(provider,project,directory,tick=tick).verify_refs(snapshot)
    return {'refs':len(snapshot['refs']),'lfs':len(snapshot['lfs_objects'])}


def inspect(row, provider, receiver, tick):
    plan=json.loads(row.plan_json); inventory=get(GitMigrationInventory,row.inventory_id); frozen=json.loads(inventory.result_json)
    previous=json.loads(row.result_json)
    result={'checks':[], 'notice':'检查覆盖率不是成功概率；网络、存储与来源仍须在正式执行时复核。',
            'rehearsal':previous.get('rehearsal','尚未执行') if row.phase=='activate' else '尚未执行'}
    if row.phase=='activate':
        result['trial_checks']=previous.get('trial_checks',previous.get('checks',[]))
        result['recheck']='running'
    row.result_json=encode(result); db.session.commit()
    def inputs():
        # 有效期限制管理员提交启动的时刻，不能把已接受的长复检中途作废。
        # 仍逐项检查输入身份；中断后过期的继续请求会由 resume 要求重新确认。
        reason = stale(row, check_expiry=row.phase != 'activate')
        require(not reason, reason, 'stale')
        return '快照一致'
    check(row,'inputs','输入快照与人员处理方式',inputs,'重新盘点或完成人员确认后重新预迁移')
    def compatibility_check():
        from .discussion_history import compatibility
        value = compatibility(plan, frozen)
        require(value['acknowledged'], '需要确认：保留来源讨论状态，目标采用 Gitea 原生合并规则', 'compatibility_required')
        return value
    check(row,'compatibility','来源与目标行为差异',compatibility_check,'核对下方受影响项目清单并整批确认')
    check(row,'receiver','接收能力与目标群组身份',lambda: receiver.check() and receiver.verify_selected_scope(plan['selection']), '检查接收端版本、凭据和群组权限')
    def source_identity():
        identity=provider.authenticate()
        require(identity['actor']==frozen['actor'] and identity['version']==frozen['version'], '来源读取身份或版本改变','stale')
        return '来源管理员与版本一致'
    from .capacity import budget, verify
    check(row,'capacity','容量、inode 与真实写入探测',lambda:verify(receiver,root(),budget(frozen,root()/'code-snapshots'/str(inventory.id))), '释放或扩容 Manager 与目标存储后重试')
    check(row,'source_identity','来源完整读取身份',source_identity,'恢复来源管理员读取权限，版本变化后重新盘点')
    check(row,'paths','目标路径与重复仓库',lambda:path_check(receiver,plan['selection'],frozen['projects'],json.loads(row.checkpoint_json)), '选择没有冲突的接收群组，保留已有仓库')
    required=rules.requirements(frozen)
    def mapping_check():
        if required:
            from .account_mapping import resolve
            raw,retained,receipt=resolve(plan['mapping_receipt']['mapping_id'],inventory,get(GiteaServer,row.server_id),plan['selection']['id'],receiver,True)
            current=rules.validate_mapping(raw,required,receiver,retained)
            require(current==plan['principal_map'] and retained==plan['retained_principals'] and receipt==plan['mapping_receipt'],'人员映射发生变化','stale')
        return '有效权限及来源身份处理已确认'
    check(row,'people','目标账号状态与有效权限',mapping_check,'回到人员确认页处理变化的账号和权限')
    from .normalize import objects
    from .assets import plan_assets
    from .packages import package_assets
    from .ci import ci_assets
    from .discussions import historical_diff
    repo_map={item['project']['id']:{'repo_id':item['project']['id']} for item in frozen['projects']}
    for item in frozen['projects']:
        project=item['project']; name=project['path']; tick()
        expected={k:v for k,v in item.items() if k not in ('code_snapshot','dependency','source_manifest')}
        def source_check():
            require(digest(runs.content_digest(Inventory(provider).inspect(project)))==digest(runs.content_digest(expected)), '来源资料在盘点后发生变化','source_changed')
            require(item.get('source_manifest') and digest(capture(provider,project))==digest(item['source_manifest']), '独立来源清单不完整或已经变化','source_changed')
            return {'objects':len(item['source_manifest']['objects']), 'files':len(item['source_manifest']['files'])}
        check(row,'source:'+str(project['id']),'来源完整性与变化检查',source_check,'确认完整读取权限、停止写入并重新盘点',name)
        def code_check():
            return verify_source_code(row,provider,project,item.get('code_snapshot'),tick)
        check(row,'code:'+str(project['id']),'Git 与 LFS 快照',code_check,'恢复代码读取权限，来源变化时重新盘点',name)
        def conversion_check():
            directory=root()/'code-snapshots'/str(inventory.id)/str(project['id'])/'repository.git'
            process=Process(root()/'preflights'/str(row.id)/'validation',tick=tick)
            normalized=objects(item,provider.kind,repository_map=repo_map,principal_map=plan['principal_map'],retained_principals=plan.get('retained_principals',{}),
                resolve_tag=lambda tag:process.run(['git','rev-parse','--verify','refs/tags/'+tag+'^{commit}'],cwd=directory).decode().strip(),
                resolve_diff=lambda position:historical_diff(process,directory,position))
            assets=plan_assets(item,provider.kind,normalized,plan['key'],provider.http.base,receiver.base)
            assets.extend(package_assets(item,provider.kind,normalized,provider.http.base))
            rule_plan=rules.convert(item,provider.kind,plan['principal_map'],normalized)
            from .merge_conditions import convert
            convert(item,provider.kind,plan['principal_map'],normalized)
            history,gaps=ci_assets(item,provider.kind,provider.http.base);assets.extend(history);normalized['pending'].extend(gaps)
            pending=[p for p in normalized['pending'] if p.get('category')!='Wiki 页面']
            require(not pending and not normalized['gaps'],encode(pending+normalized['gaps'])[:1600] or '存在未支持内容','unsupported')
            return verify_plan(item['source_manifest'],normalized,assets,rule_plan)
        check(row,'conversion:'+str(project['id']),'资料转换、规则与文件覆盖',conversion_check,'查看具体资料类型，补齐接收能力；禁止忽略缺失项',name)
    return all(c['status']=='passed' for c in json.loads(row.result_json)['checks'])


def execute(row, provider, tick):
    plan=json.loads(row.plan_json)
    folder=root()/'preflights'/str(row.id); folder.mkdir(parents=True,exist_ok=True)
    receiver=Receiver(get(GiteaServer,row.server_id),Process(folder/'checks',tick=tick),folder)
    try:
        if row.phase=='cancel':
            with target_guard(row.server_id,migration=True,git_preflight_id=row.id):
                receiver.check()
                require({k:receiver.actor[k] for k in ('id','login')} == plan['target_actor'], '目标管理员身份已变化，不能关闭原接收会话', 'identity')
                for progress in json.loads(row.checkpoint_json).values():
                    if not isinstance(progress,dict): continue
                    intent=progress.get('creating_repo',{})
                    repo_id=progress.get('repo_id')
                    if not repo_id and intent:
                        found=receiver.repository(receiver.actor['login'],intent['name'],missing=True)
                        if found:
                            require(found.get('description')==intent['marker'], '回执丢失且目标标记不符，不能接管', 'identity')
                            repo_id=found['id']
                    if repo_id:
                        reply=receiver.api('POST','/governance/migration-sessions/'+progress['key']+'/abort',{'repo_id':repo_id,'marker':intent.get('marker','')})
                        require(reply.get('repo_id')==repo_id and reply.get('state')=='abandoned' and reply.get('archived') is True, '隔离会话关闭回执不符，继续保留互斥', 'identity')
                row.status='cancelled';row.error='';row.error_code='';row.stage='预迁移已结束，隔离副本保留归档';db.session.commit()
            return
        with target_guard(row.server_id,migration=True,git_preflight_id=row.id):
            ready=inspect(row,provider,receiver,tick)
            if not ready:
                if row.phase=='activate':
                    result=json.loads(row.result_json);result['recheck']='blocked';row.result_json=encode(result)
                row.status='blocked';row.stage='存在阻断项，未创建正式迁移任务';db.session.commit();return
            if row.phase=='activate':
                checkpoints=json.loads(row.checkpoint_json)
                for project in json.loads(get(GitMigrationInventory,row.inventory_id).result_json)['projects']:
                    item=checkpoints.get(str(project['project']['id']),{})
                    require(item.get('sealed') and item.get('repo_id'), '隔离副本缺失，必须重新预迁移', 'stale')
                    def verify_staging():
                        receiver.verify_objects(item['key'],item,lambda _:None)
                        state=receiver.api('GET','/governance/migration-sessions/'+item['key']+'/status')
                        require(state.get('state') in ('sealed','handed_off') and state.get('repo_id')==item['repo_id'],'封存副本发生变化，必须重新预迁移','stale')
                        import secrets
                        repo=receiver.api('GET','/repositories/'+str(item['repo_id']))
                        require(repo['id']==item['repo_id'] and repo['private'] and (not item.get('prepared') or (repo.get('full_path') or repo['full_name'])==item['final_path']), '隔离副本所属空间或可见性改变', 'stale')
                        receiver.verify_code(item['snapshot'],repo,folder/('recheck-'+secrets.token_hex(8)),item,lambda _:None)
                        receiver.verify_assets(item)
                        receiver.verify_wiki(item,repo,folder/('wiki-recheck-'+secrets.token_hex(8)))
                        return '封存对象、代码、LFS、Wiki 与附件重新回读通过'
                    if not check(row,'staging:'+str(project['project']['id']),'正式启动前隔离副本复检',verify_staging,'结束本次预迁移，重新生成并核验隔离副本',project['project']['path']):
                        result=json.loads(row.result_json);result['recheck']='blocked';row.result_json=encode(result)
                        row.status='blocked';row.stage='隔离副本复检失败，未创建正式任务';db.session.commit();return
        if row.phase=='activate':
            # request_start 已在有效期内接受停写确认；复检耗时不使该请求失效。
            activation=plan['activation']; request={**plan['request'],**activation,'preflight_id':row.id}
            runs.enqueue(request,row.actor_id,preflight=row)
            result=json.loads(row.result_json);result['rehearsal']='passed';row.result_json=encode(result)
            result['recheck']='passed';row.result_json=encode(result)
            row.stage='复检通过，已创建正式迁移任务';db.session.commit();return
        runs.execute(row,provider,tick,rehearsal=True)
        result=json.loads(row.result_json)
        checkpoints=json.loads(row.checkpoint_json)
        prepared=row.status=='running' and checkpoints.get('batch_prepared') is True
        result['rehearsal']='passed' if prepared else 'blocked'
        for key,p in checkpoints.items():
            if key.isdigit():
                result['checks'].append({'key':'trial:'+key,'title':'隔离试迁移与目标回读','project':key,'status':'passed' if prepared else 'blocked',
                    'detail':{'source':p.get('source_verified'),'git':p.get('code_verified',False),'objects':p.get('objects_verified',False),'assets':len(p.get('assets',{}))},
                    'remedy':'' if prepared else '检查未完成资料和隔离副本，结束后重新预迁移'})
        row.result_json=encode(result)
        if prepared:
            row.status='ready';row.expires_at=datetime.utcnow()+timedelta(minutes=30)
            row.stage='最终形态已验证，整批隔离等待开放'
        else:row.status='blocked'
        db.session.commit()
    finally:receiver.close()


def record_failure(row, message, code):
    result=json.loads(row.result_json)
    result.setdefault('checks',[]).append({'key':'execution','title':row.stage,'status':'blocked','detail':message,'code':code,
        'remedy':'处理该阻断项后结束本次预迁移，再重新执行；正式任务尚未创建'})
    result['recheck' if row.phase=='activate' else 'rehearsal']='blocked';row.result_json=encode(result)


def resume(ident):
    with file_lock(root()/'enqueue.lock',blocking=True):
        row=get(GitMigrationPreflight,ident)
        require(row.status in ('blocked','interrupted') and row.phase!='cancel','该预迁移不能继续','conflict',409)
        if row.expires_at and row.expires_at <= datetime.utcnow():
            # 到期后可以恢复数据准备，但必须重新取得检查结果与人工停写确认。
            row.expires_at=None;row.phase='trial'
            plan=json.loads(row.plan_json);plan.pop('activation',None);row.plan_json=encode(plan)
        require(not stale(row),stale(row),'stale',409)
        row.status='queued';row.error='';row.error_code='';row.stage='等待从原检查点恢复';db.session.commit();return row
