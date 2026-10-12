"""审批管理状态和只读检查；生产强制启用始终受发布阻断保护。"""
from contextlib import closing
from datetime import datetime
import hashlib
import json
import secrets
import time

from models import db, GiteaServer, ApprovalPolicy, ApprovalAudit
from .gitea import Reader
from .timeline import validate_reference
from .rules import ApprovalError, encode, normalize, digest, evaluate

RELEASE_BLOCKERS = ['APR-RACE-001：P2 已知竞态尚未修复', '完整接入矩阵及现场验收尚未完成']
POST_RELEASE_CHECKS = ['多用户并发及容量验收', '并发接入边界回归', '影子模式持续 24 小时', '隔离强制模式持续 24 小时']


def audit(policy, action, detail):
    from access.scopes import actor_name
    db.session.add(ApprovalAudit(policy_id=policy.id, actor=actor_name(), action=action, detail_json=encode(detail)))


def serialize(policy):
    return {'id': policy.id, 'server_id': policy.server_id, 'repo_id': policy.repo_id,
            'repo_name': policy.repo_name, 'owner_id': policy.owner_id,
            'rule': json.loads(policy.rule_json), 'revision': policy.revision, 'mode': policy.mode,
            'preview': json.loads(policy.preview_json), 'grandfather_count': len(json.loads(policy.grandfather_json)),
            'release_allowed': False, 'release_blockers': RELEASE_BLOCKERS,
            'post_release_checks': POST_RELEASE_CHECKS,
            'updated_at': policy.updated_at.isoformat() + 'Z'}


def reader(policy, seconds=8):
    server = db.session.get(GiteaServer, policy.server_id)
    if not server:
        raise ApprovalError('Gitea 实例已删除')
    return Reader(server, seconds)


def inspect_policy(policy):
    rule = json.loads(policy.rule_json)
    with closing(reader(policy, 25)) as client:
        context = client.context(policy, rule)
        path = client.path(context['repo'])
        prs = client.pages(path + '/pulls', {'state': 'all'})
        if prs:
            sample=prs[0]
            validate_reference(client.metadata.snapshot(policy.repo_id,sample['number']),
                               policy.repo_id,sample['number'],sample['id'])
        else:
            reference=client.metadata.snapshot(policy.repo_id,1)
            if reference.get('pr_id') is not None or reference.get('count')!=0:
                raise ApprovalError('原生 PR 列表与只读数据库定位不一致')
        # 存量身份绑定原生 PR ID 和序号，不能仅按 SHA 或事件到达顺序豁免。
        identities = sorted([{'id': p['id'], 'number': p['number'], 'created_at': p['created_at']} for p in prs], key=lambda p: p['id'])
        confirm = client.pages(path + '/pulls', {'state': 'all'})
        if identities != sorted([{'id': p['id'], 'number': p['number'], 'created_at': p['created_at']} for p in confirm], key=lambda p: p['id']):
            raise ApprovalError('预检期间 PR 创建边界变化，未完成接入预检')
        again = client.repository(policy)
        if [again['id'], again['full_name'], again['owner']['id']] != [policy.repo_id, policy.repo_name, policy.owner_id]:
            raise ApprovalError('预检期间仓库身份变化')
        return {'checked_at': time.time(), 'revision': policy.revision, 'fingerprint': context['fingerprint'],
                'snapshot': identities, 'native_rules': context['protections'],
                'groups': [{'team_id': i, 'name': t['name'], 'members_count': len(t['members'])}
                           for i, t in context['teams'].items() if i in {g['team_id'] for g in rule['groups']}],
                'changes': ['新增独立审批 Hook（默认影子、不同步等待外部服务）', '新增 PR 只读展示资源和页脚片段'],
                'production_enforcement_allowed': False,
                'pending_checks': ['实际持久卷和其他 Hook', '模板并发修改及热重载真实页面', '代理同源路径及会话验证',
                                   '正式切换的并发创建 PR 边界', *RELEASE_BLOCKERS]}


def status(policy, number, expected=None, *, deadline=None):
    rule = json.loads(policy.rule_json)
    revision = policy.revision
    mode, token_hash = policy.mode, policy.hook_token_hash
    seconds=8 if deadline is None else min(8,deadline-time.monotonic())
    if seconds<=0:raise ApprovalError('审批核对超出本次请求预算')
    with closing(reader(policy,seconds)) as client:
        context = client.context(policy, rule, number)
        pr = context['pr']
        branch = pr['base']['ref']
        grandfather = any(p['id'] == pr['id'] and p['number'] == number for p in json.loads(policy.grandfather_json))
        if expected:
            if expected['pr_id'] != pr['id'] or expected['ref'] != 'refs/heads/' + branch or expected['old'] != pr['base']['sha']:
                raise ApprovalError('Hook 的 PR、目标分支或基线与原生状态不匹配')
        if branch not in rule['branches']:
            result = {'satisfied': False, 'applicable': False, 'missing': [], 'head': pr['head']['sha']}
        else:
            result = evaluate(rule, pr, context['reviews'], context['eligible'][branch], context['teams'], context['native'][branch])
        if policy.mode == 'isolated_enforced':
            preview = json.loads(policy.preview_json)
            if context['fingerprint'] != preview.get('fingerprint'):
                raise ApprovalError('规则、成员或资格与启用预检不一致，需要重新预检；未沿用旧通过结果')
        db.session.refresh(policy)
        if policy.revision != revision or policy.mode != mode or policy.hook_token_hash != token_hash:
            raise ApprovalError('判定期间规则版本或接入状态变化')
        if deadline is not None and time.monotonic()>=deadline:
            raise ApprovalError('审批核对超出本次请求预算，未返回过期结果')
        result.update(policy_id=policy.id, repository_id=policy.repo_id, number=number, pr_id=pr['id'],
                      revision=revision, mode=policy.mode, grandfathered=grandfather,
                      checked_at=time.time(), valid_until=time.time()+15, base=pr['base']['sha'],
                      closed=pr['state']!='open' or bool(pr['merged']),
                      release_allowed=False, name=rule['name'], known_defects=['APR-RACE-001'])
        return result


def verify_isolation(policy):
    """只认可验收工具创建且本次进程显式绑定的容器，不接受请求体的测试标志。"""
    import os
    from services.docker_service import _get_client
    expected = os.environ.get('GM_APPROVAL_ISOLATION_ID', '')
    server = db.session.get(GiteaServer, policy.server_id)
    if not expected or not server or not server.is_local:
        raise ApprovalError('生产强制启用被发布门禁阻断；仅允许有独立标记的本地验收实例')
    info = _get_client().containers.get(server.gitea_container).attrs
    if info.get('Config', {}).get('Labels', {}).get('com.gitea-manager.approval-feasibility') != expected:
        raise ApprovalError('隔离实例标记不匹配，拒绝强制启用')
    return expected


def prepare_bundle(policy, data):
    from .bundle import build_bundle
    mode = data.get('mode', 'shadow')
    if mode not in ('shadow', 'isolated_enforced'):
        raise ApprovalError('生产强制审批尚未获准发布')
    if policy.mode not in ('draft', 'disabled', 'shadow'):
        raise ApprovalError('已有安装准备或强制门禁，请先核对并完成安装或明确停用；不能重复导出更换凭据')
    previous_mode, previous_token, revision = policy.mode, policy.hook_token_hash, policy.revision
    import os
    isolation = verify_isolation(policy) if mode == 'isolated_enforced' or os.environ.get('GM_APPROVAL_ISOLATION_ID') else ''
    preview = json.loads(policy.preview_json)
    if not preview or preview.get('revision') != policy.revision or time.time() - preview.get('checked_at', 0) > 300:
        raise ApprovalError('请先完成当前版本预检，预检有效期为五分钟')
    current = inspect_policy(policy)
    if current['fingerprint'] != preview['fingerprint'] or current['snapshot'] != preview['snapshot']:
        raise ApprovalError('预检后规则、成员或 PR 边界变化，请重新预检')
    token = secrets.token_urlsafe(32)
    policy.hook_token_hash = hashlib.sha256(token.encode()).hexdigest()
    policy.grandfather_json = encode(current['snapshot'])
    policy.preview_json = encode(current)
    # 导出不是安装成功，运行模式留在准备状态；Hook 对未激活状态拒绝强制放行。
    policy.mode = 'prepared_' + mode
    audit(policy, 'bundle.prepare', {'mode': mode, 'revision': policy.revision, 'grandfather_count': len(current['snapshot'])})
    bundle = build_bundle(policy, token, data.get('gate_url', ''), isolation, mode)
    # 网络预检期间不能持有写锁；生成包后使用旧状态与旧凭据一次性领取安装。
    values = {'hook_token_hash': policy.hook_token_hash, 'grandfather_json': policy.grandfather_json,
              'preview_json': policy.preview_json, 'mode': policy.mode}
    db.session.expire(policy)
    with db.session.no_autoflush:
        changed = ApprovalPolicy.query.filter_by(id=policy.id, mode=previous_mode,
                    revision=revision, hook_token_hash=previous_token).update(values)
    if changed != 1:
        raise ApprovalError('其他任务已改变安装状态，未交付过期组件包')
    db.session.commit()
    return bundle
