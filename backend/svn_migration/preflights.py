"""预检历史、有效性与仓库结果；不在读取时重新探测或写入来源。"""
from datetime import datetime
import json
from sqlalchemy import func
from models import db
from .common import MigrationError, encode
from .models import (SvnMigrationPlan, SvnPreflight, SvnPreflightRepository,
                     SvnMigrationItem, SvnMigrationRun)


def begin(plan):
    row = SvnPreflight(plan_id=plan.id, plan_name=plan.name, plan_version=plan.version,
                       server_id=plan.server_id, config_json=plan.config_json)
    db.session.add(row)
    db.session.flush()
    for item in json.loads(plan.config_json)['repositories']:
        db.session.add(SvnPreflightRepository(preflight_id=row.id, repository_id=item['id']))
    return row


def finish(plan, row, preview, error=''):
    row.status, row.preview_json, row.error = plan.status, encode(preview), error
    row.completed_at = datetime.utcnow()


def current(row, plan=None):
    from .service import input_state
    plan = plan or db.session.get(SvnMigrationPlan, row.plan_id)
    if not plan or plan.version != row.plan_version or plan.server_id != row.server_id:
        return False
    preview = json.loads(row.preview_json)
    if not preview.get('inputs'):
        return False
    try:
        return preview['inputs'] == input_state(plan)
    except MigrationError:
        return False


def public(row, detail=False):
    from .service import public_preview
    preview = json.loads(row.preview_json)
    result = {'id': row.id, 'plan_id': row.plan_id, 'plan_name': row.plan_name,
              'plan_version': row.plan_version, 'server_id': row.server_id,
              'status': row.status, 'current': current(row), 'error': row.error,
              'created_at': row.created_at.isoformat(),
              'completed_at': row.completed_at.isoformat() if row.completed_at else None,
              'issue_count': len(preview.get('issues', []))}
    if detail:
        # 只从当次快照取名称和布局，后续编辑不能冒充历史输入。
        result['preview'] = public_preview(preview)
        result['config'] = json.loads(row.config_json)
        result['previous_id'] = db.session.query(func.max(SvnPreflight.id)).filter(
            SvnPreflight.plan_id == row.plan_id, SvnPreflight.id < row.id).scalar()
        result['next_id'] = db.session.query(func.min(SvnPreflight.id)).filter(
            SvnPreflight.plan_id == row.plan_id, SvnPreflight.id > row.id).scalar()
    return result


def latest_for_plans(ids):
    if not ids:
        return {}
    newest = db.session.query(func.max(SvnPreflight.id)).filter(SvnPreflight.plan_id.in_(ids)).group_by(SvnPreflight.plan_id)
    return {r.plan_id: r for r in SvnPreflight.query.filter(SvnPreflight.id.in_(newest)).all()}


def repository_results(rows):
    """按仓库批量读取最后一次已完成预检和最后一次实际执行，禁止互相替代。"""
    ids = [r.id for r in rows]
    if not ids:
        return {}
    latest = db.session.query(func.max(SvnPreflightRepository.preflight_id)).join(
        SvnPreflight, SvnPreflight.id == SvnPreflightRepository.preflight_id).filter(
        SvnPreflightRepository.repository_id.in_(ids), SvnPreflight.completed_at.isnot(None)).group_by(SvnPreflightRepository.repository_id)
    checks = SvnPreflight.query.filter(SvnPreflight.id.in_(latest)).all()
    latest_run = db.session.query(func.max(SvnMigrationItem.id)).filter(
        SvnMigrationItem.repository_id.in_(ids)).group_by(SvnMigrationItem.repository_id)
    items = SvnMigrationItem.query.filter(SvnMigrationItem.id.in_(latest_run)).all()
    # 成功迁移事实即使后来另一次执行失败仍保留，不能把已创建仓库写成从未迁移。
    successes = db.session.query(SvnMigrationItem).filter(
        SvnMigrationItem.repository_id.in_(ids), SvnMigrationItem.status == 'success').order_by(SvnMigrationItem.id.desc()).all()
    result = {ident: {} for ident in ids}
    for item in items:
        run = db.session.get(SvnMigrationRun, item.run_id)
        result[item.repository_id]['migration'] = {'run_id': item.run_id, 'status': item.status,
            'server_id': run.server_id, 'result': {k: v for k, v in json.loads(item.result_json).items()
                                                if k not in ('提交映射', '属性', '修订覆盖')}}
    for item in successes:
        if 'last_success' not in result[item.repository_id]:
            result[item.repository_id]['last_success'] = {'run_id': item.run_id,
                'target': json.loads(item.result_json).get('目标', '')}
    for check in sorted(checks, key=lambda r: r.id):
        summary = public(check)
        preview = json.loads(check.preview_json)
        inspected = {r['repository_id'] for r in preview.get('repositories', [])}
        # 全局目标/连接错误适用于整个计划，不能把未检查仓库标为通过。
        global_issues = [r for r in preview.get('issues', []) if not r.get('repository_id')]
        for choice in json.loads(check.config_json)['repositories']:
            ident = choice['id']
            if ident not in result:
                continue
            issues = global_issues + [r for r in preview.get('issues', []) if r.get('repository_id') == ident]
            result[ident]['preflight'] = dict(summary, issues=issues,
                repository_status='blocked' if issues else ('ready' if ident in inspected else 'unknown'))
    return result


def backfill():
    """升级保留旧版最后一份预览；已被旧版覆盖的更早结果不虚构。"""
    known = db.session.query(SvnPreflight.plan_id)
    for plan in SvnMigrationPlan.query.filter(~SvnMigrationPlan.id.in_(known)).all():
        preview = json.loads(plan.preview_json)
        if not preview:
            continue
        row = begin(plan)
        finish(plan, row, preview, plan.error)
        if preview.get('created_at'):
            row.created_at = datetime.fromisoformat(preview['created_at'])
    db.session.commit()
