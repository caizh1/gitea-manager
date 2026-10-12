"""权限迁移只确认可证明的归属；未知历史保持平台专属。"""
import json
import uuid
from models import db, GiteaServer, PolicyTemplate, PolicyBatch, ApprovalPolicy, CommitMessageRule, CommitGateAssignment
from access.scopes import bind_resource, server_epoch, ResourceScope, repositories_for


def initialize_schema():
    columns = {row[1] for row in db.session.execute(db.text('PRAGMA table_info(gitea_servers)'))}
    if 'access_epoch' not in columns:
        db.session.execute(db.text("ALTER TABLE gitea_servers ADD COLUMN access_epoch VARCHAR(64) NOT NULL DEFAULT ''"))
    for row in db.session.execute(db.text("SELECT id FROM gitea_servers WHERE access_epoch='' OR access_epoch IS NULL")).all():
        db.session.execute(db.text('UPDATE gitea_servers SET access_epoch=:epoch WHERE id=:id'), {'epoch': uuid.uuid4().hex, 'id': row[0]})
    from models import PolicyRepoResult
    for batch in PolicyBatch.query.filter(PolicyBatch.status.in_(('preview_queued', 'apply_queued', 'previewing', 'applying'))).all():
        if not json.loads(batch.scope_json or '{}').get('access_actor'):
            batch.status = 'interrupted'
            for row in PolicyRepoResult.query.filter(PolicyRepoResult.batch_id == batch.id,
                                                     PolicyRepoResult.status.in_(('pending', 'running'))):
                row.status = 'interrupted'
                row.error = '旧任务未记录可复核的个人授权，请重新预览和提交'
    db.session.commit()


def migrate_binding(binding):
    """平台明确同步组织时核对；已封存记录不会自动重新授予。"""
    from services.policy_gitea import PolicyGitea
    server = db.session.get(GiteaServer, binding.server_id)
    repos = repositories_for(binding)
    for template in PolicyTemplate.query.filter_by(server_id=server.id, organization_id=binding.organization_id).all():
        bind_resource('policy_template', template.id, server.id, binding.organization_id, migration=True, actor={})
    for policy in ApprovalPolicy.query.filter_by(server_id=server.id, owner_id=binding.organization_id).all():
        if policy.repo_id in repos:
            bind_resource('approval_policy', policy.id, server.id, binding.organization_id, [policy.repo_id], migration=True, actor={})
    for batch in PolicyBatch.query.filter_by(server_id=server.id).all():
        scope = json.loads(batch.scope_json or '{}')
        if scope.get('organization', {}).get('id') != binding.organization_id:
            continue
        from models import PolicyRepoResult
        ids = [r.repo_id for r in PolicyRepoResult.query.filter_by(batch_id=batch.id)]
        if ids and all(i in repos for i in ids):
            bind_resource('policy_batch', batch.id, server.id, binding.organization_id, ids, migration=True, actor={})
    # 共享规则复制定义并重绑本组织引用，实际 Hook 内容不变。
    for rule in CommitMessageRule.query.filter_by(server_id=server.id).all():
        assignments = CommitGateAssignment.query.filter_by(rule_id=rule.id).all()
        owned = [a for a in assignments if a.repo_id in repos and repos[a.repo_id]['full_name'] == a.repo_name]
        if not owned:
            continue
        record = ResourceScope.query.filter_by(kind='commit_rule', resource_key=str(rule.id)).first()
        if record and (record.archived or record.binding_id != binding.id):
            continue
        if len(owned) != len(assignments):
            copy = CommitMessageRule(server_id=rule.server_id, name=rule.name, pattern=rule.pattern,
                                     reject_message=rule.reject_message, enabled=rule.enabled)
            db.session.add(copy)
            db.session.flush()
            for assignment in owned:
                assignment.rule_id = copy.id
            rule = copy
        bind_resource('commit_rule', rule.id, server.id, binding.organization_id, migration=True, actor={})
    db.session.commit()
    from flask import current_app
    from ai_review.store import Store
    state = Store(current_app.config.get('AI_REVIEW_DB'))
    for repository in state.repos():
        if repository.get('access_archived') or repository['server_id'] != server.id or repository['repo_id'] not in repos:
            continue
        # 旧版本没有组织快照；仓库已转移或改名时不能将原历史和样本授予新组织。
        if repository.get('repo_name') != repos[repository['repo_id']].get('full_name'):
            continue
        record = bind_resource('ai_repository', repository['id'], server.id, binding.organization_id,
                               [repository['repo_id']], migration=True, actor={})
        if record:
            state.repo_status(repository['id'], access_scope={'binding_id': binding.id, 'instance_epoch': server_epoch(server),
                              'organization_id': binding.organization_id, 'server_id': server.id})
    db.session.commit()
