"""策略任务持久化执行。进程间文件锁串行化实例，数据库统一分配请求时隙。"""
import fcntl
import json
import logging
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from models import db, GiteaServer, PolicyBatch, PolicyRepoResult
from services.policy_gitea import PolicyGitea, connection_hash
from services.policy_rules import PolicyError, canonical_current, encode, plan_changes
from services.policy_scope import validate_repo_scope
from access.errors import AccessError

QUEUED = ('preview_queued', 'apply_queued')
RUNNING = ('previewing', 'applying')
_started = set()


@contextmanager
def instance_lock(app, server_id):
    # SQLite 部署必须共享数据库所在的同一持久卷；锁随进程退出由操作系统释放。
    uri = app.config['SQLALCHEMY_DATABASE_URI']
    base = app.config.get('POLICY_LOCK_DIR')
    if not base:
        base = str(Path(uri.removeprefix('sqlite:///')).resolve().parent / '.policy-locks') if uri.startswith('sqlite:///') and ':memory:' not in uri else str(Path(tempfile.gettempdir()) / 'gitea-manager-policy-locks')
    Path(base).mkdir(parents=True, exist_ok=True)
    with open(Path(base) / f'{server_id}.lock', 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def client_for(batch):
    server = db.session.get(GiteaServer, batch.server_id, populate_existing=True)
    if not server or connection_hash(server) != batch.connection_hash:
        raise PolicyError('实例连接或 Token 已变化，请重新预览')
    return PolicyGitea(server)


def save_result(result, **values):
    for key, value in values.items():
        setattr(result, key, value)
    result.updated_at = datetime.utcnow()
    db.session.commit()


def preview_repo(batch_id, result_id):
    result = db.session.get(PolicyRepoResult, result_id)
    try:
        batch = db.session.get(PolicyBatch, batch_id)
        client = client_for(batch)
        repo = client.repo(result.repo_id)
        scope = json.loads(batch.scope_json or '{}')
        if scope.get('access_actor'):
            from access.scopes import check_task_actor
            check_task_actor(scope['access_actor'], batch.server_id, result.repo_id)
        validate_repo_scope(client, repo, scope)
        save_result(result, repo_name=repo['full_name'], repo_identity_json=encode(client.identity(repo)))
        rules = json.loads(batch.template_json)
        current = client.protections(repo)
        save_result(result, before_json=encode(canonical_current(current)))
        access = client.validate_people(repo, rules, scope.get('identities', {}))
        before, desired, changes = plan_changes(current, rules)
        matched = client.matching_branches(repo, rules)
        save_result(result, before_json=encode(before), desired_json=encode(desired), diff_json=encode(changes),
                    matches_json=encode(matched), access_json=encode(access), status='changes' if changes else 'unchanged')
    except Exception as exc:
        db.session.rollback()
        result = db.session.get(PolicyRepoResult, result_id)
        save_result(result, status='blocked', error=str(exc) if isinstance(exc, (PolicyError, AccessError)) else '预览失败，请查看服务端诊断')
        if not isinstance(exc, (PolicyError, AccessError)):
            logging.exception('策略预览内部错误，记录 %s', result_id)


def apply_repo(batch_id, result_id):
    result = db.session.get(PolicyRepoResult, result_id)
    client = repo = None
    attempted = False
    try:
        batch = db.session.get(PolicyBatch, batch_id)
        client = client_for(batch)
        repo = client.repo(result.repo_id)
        scope = json.loads(batch.scope_json or '{}')
        if scope.get('access_actor'):
            from access.scopes import check_task_actor
            check_task_actor(scope['access_actor'], batch.server_id, result.repo_id)
        validate_repo_scope(client, repo, scope)
        if client.identity(repo) != json.loads(result.repo_identity_json):
            raise PolicyError('仓库名称、所属组织或默认分支已变化，请重新预览')
        actual = canonical_current(client.protections(repo))
        save_result(result, actual_json=encode(actual))
        if actual != json.loads(result.before_json):
            raise PolicyError('仓库规则已在预览后发生变化，请重新预览')
        rules = json.loads(batch.template_json)
        access = client.validate_people(repo, rules, scope.get('identities', {}))
        if access != json.loads(result.access_json or '{}'):
            raise PolicyError('名单对应的仓库权限已在预览后变化，请重新预览')
        changes = json.loads(result.diff_json)
        desired = json.loads(result.desired_json)
        expected = actual
        operations = []
        for change in changes:
            fresh_repo = client.repo(result.repo_id)
            if client.identity(fresh_repo) != json.loads(result.repo_identity_json):
                raise PolicyError('执行期间仓库发生转移或改名，已停止后续写入')
            validate_repo_scope(client, fresh_repo, scope)
            # 每次写入前复查所有规则，限制外部管理员并发修改造成的覆盖窗口。
            if canonical_current(client.protections(repo)) != expected:
                raise PolicyError('执行期间规则发生外部修改，已停止后续写入')
            client_for(batch)
            if scope.get('access_actor'):
                check_task_actor(scope['access_actor'], batch.server_id, result.repo_id)
            name = change['rule_name']
            wanted = next(r for r in desired if r['rule_name'] == name)
            action = change['action']
            op = {'rule_name': name, 'action': action, 'status': 'requesting'}
            operations.append(op)
            save_result(result, operations_json=encode(operations))
            attempted = True
            path = client.path(repo) + '/branch_protections'
            try:
                if action == 'create':
                    client.request('POST', path, json=wanted)
                    if wanted['enable_force_push'] and not wanted['enable_push']:
                        # 1.26.4 POST 会清掉这组独立强推选项，PATCH 与原生网页则支持。
                        # 先核对唯一允许的中间状态，再执行补全；响应异常不会进入补全或重放。
                        # wanted 是已校验的 API 快照，不含 Manager 的 team_org 元数据。
                        interim = canonical_current([{**wanted, 'enable_force_push': False}])[0]
                        interim_expected = sorted(expected + [interim], key=lambda r: r['rule_name'])
                        interim_actual = canonical_current(client.protections(repo))
                        save_result(result, actual_json=encode(interim_actual))
                        if interim_actual != interim_expected:
                            raise PolicyError('创建规则的中间状态与预期不符，未补全强推配置，请重新预览')
                        op['status'] = 'completing_force_push'
                        save_result(result, operations_json=encode(operations))
                        if client.identity(client.repo(result.repo_id)) != json.loads(result.repo_identity_json):
                            raise PolicyError('补全强推前仓库发生转移或改名，已停止写入')
                        client_for(batch)
                        if scope.get('access_actor'):
                            check_task_actor(scope['access_actor'], batch.server_id, result.repo_id)
                        client.request('PATCH', path + '/' + quote(name, safe=''), json={k: v for k, v in wanted.items() if k != 'rule_name'})
                else:
                    client.request('PATCH', path + '/' + quote(name, safe=''), json={k: v for k, v in wanted.items() if k != 'rule_name'})
            except PolicyError:
                # 不重放写请求；由随后一次读取判断服务器是否已经完成写入。
                op['status'] = 'response_uncertain'
                save_result(result, operations_json=encode(operations))
                raise
            expected = sorted([r for r in expected if r['rule_name'] != name] + [wanted], key=lambda r: r['rule_name'])
            actual = canonical_current(client.protections(repo))
            save_result(result, actual_json=encode(actual))
            if client.identity(client.repo(result.repo_id)) != json.loads(result.repo_identity_json):
                raise PolicyError('写入期间仓库发生转移或改名，请重新预览确认实际状态')
            if actual != expected:
                op['status'] = 'verification_failed'
                save_result(result, operations_json=encode(operations))
                raise PolicyError('Gitea 回读与预期不一致，可能存在名单过滤或外部修改，已停止后续写入')
            op['status'] = 'verified'
            save_result(result, operations_json=encode(operations))
        final_repo = client.repo(result.repo_id)
        validate_repo_scope(client, final_repo, scope)
        if client.identity(final_repo) != json.loads(result.repo_identity_json):
            raise PolicyError('最终核验时仓库身份已变化，请重新预览')
        if client.validate_people(final_repo, rules, scope.get('identities', {})) != access:
            raise PolicyError('执行期间名单权限发生变化，请重新预览')
        save_result(result, actual_json=encode(actual), status='success' if changes else 'unchanged', error='')
    except Exception as exc:
        db.session.rollback()
        result = db.session.get(PolicyRepoResult, result_id)
        message = str(exc) if isinstance(exc, (PolicyError, AccessError)) else '执行失败，请查看服务端诊断'
        if attempted and client and repo:
            try:
                actual = canonical_current(client.protections(repo))
                if actual == json.loads(result.desired_json):
                    fresh_repo = client.repo(result.repo_id)
                    if client.identity(fresh_repo) != json.loads(result.repo_identity_json):
                        raise PolicyError('仓库身份已变化，不能确认结果')
                    validate_repo_scope(client, fresh_repo, scope)
                    if client.validate_people(fresh_repo, rules, scope.get('identities', {})) != json.loads(result.access_json or '{}'):
                        raise PolicyError('仓库权限已变化，不能确认结果')
                    save_result(result, actual_json=encode(actual), status='success', error='请求响应异常，但全部规则已回读核验成功')
                    return
                save_result(result, actual_json=encode(actual))
                status = 'partial' if actual != json.loads(result.before_json) else 'failed'
            except Exception:
                status = 'uncertain'
                message += '；当前规则或身份权限未能完整确认，结果待确认，请重新预览'
        else:
            status = 'blocked'
        save_result(result, status=status, error=message)
        if not isinstance(exc, (PolicyError, AccessError)):
            logging.exception('策略执行内部错误，记录 %s', result_id)


def recover_interrupted(server_id):
    for batch in PolicyBatch.query.filter(PolicyBatch.server_id == server_id, PolicyBatch.status.in_(RUNNING)).all():
        for row in PolicyRepoResult.query.filter(PolicyRepoResult.batch_id == batch.id, PolicyRepoResult.status.in_(('pending', 'running'))).all():
            row.status = 'interrupted'
            row.error = '执行进程已中断；保留已完成记录，请重新预览以确认仓库实际状态'
            row.updated_at = datetime.utcnow()
        batch.status = 'interrupted'
        batch.error = '执行进程已中断，未自动重复下发规则'
        batch.completed_at = datetime.utcnow()
    db.session.commit()


def run_batch(app, batch_id):
    batch = db.session.get(PolicyBatch, batch_id)
    preview = batch.status == 'preview_queued'
    claimed = PolicyBatch.query.filter_by(id=batch.id, status=batch.status).update({'status': 'previewing' if preview else 'applying'})
    db.session.commit()
    if not claimed:
        return
    try:
        client_for(batch).check_version()
        ids = [x.id for x in PolicyRepoResult.query.filter_by(batch_id=batch.id, status='pending').all()]
        def execute(result_id):
            with app.app_context():
                try:
                    row = db.session.get(PolicyRepoResult, result_id)
                    save_result(row, status='running')
                    (preview_repo if preview else apply_repo)(batch_id, result_id)
                finally:
                    db.session.remove()
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='policy-repository') as pool:
            list(pool.map(execute, ids))
        db.session.expire_all()
        batch = db.session.get(PolicyBatch, batch_id)
        if preview:
            batch.status = 'ready'
        else:
            failed = PolicyRepoResult.query.filter(PolicyRepoResult.batch_id == batch_id,
                PolicyRepoResult.status.notin_(('success', 'unchanged'))).count()
            batch.status = 'completed_with_errors' if failed else 'completed'
        batch.completed_at = datetime.utcnow()
        db.session.commit()
        if not preview and batch.status == 'completed_with_errors':
            from services.alert_service import create_alert
            create_alert('policy_failed', batch.server_id, f'策略任务 #{batch.id} 有 {failed} 个仓库未成功，请在仓库策略中心查看原因', batch.id)
    except Exception as exc:
        db.session.rollback()
        batch = db.session.get(PolicyBatch, batch_id)
        batch.status = 'interrupted'
        batch.error = str(exc) if isinstance(exc, (PolicyError, AccessError)) else '批量任务内部异常，请重新预览'
        batch.completed_at = datetime.utcnow()
        PolicyRepoResult.query.filter(PolicyRepoResult.batch_id == batch_id,
            PolicyRepoResult.status.in_(('pending', 'running'))).update({'status': 'interrupted', 'error': batch.error})
        db.session.commit()
        if not isinstance(exc, (PolicyError, AccessError)):
            logging.exception('策略批量任务异常 %s', batch_id)


def dispatch_once(app):
    with app.app_context():
        servers = [x[0] for x in db.session.query(PolicyBatch.server_id).filter(PolicyBatch.status.in_(QUEUED + RUNNING)).distinct().all()]
        for server_id in servers:
            with instance_lock(app, server_id) as locked:
                if not locked:
                    continue
                recover_interrupted(server_id)
                batch = PolicyBatch.query.filter(PolicyBatch.server_id == server_id, PolicyBatch.status.in_(QUEUED)).order_by(PolicyBatch.id).first()
                if batch:
                    run_batch(app, batch.id)
                    return True
    return False


def start_policy_worker(app):
    key = (os.getpid(), app.config['SQLALCHEMY_DATABASE_URI'])
    if key in _started:
        return
    _started.add(key)
    def loop():
        while True:
            try:
                from services.upgrade_maintenance import active
                if not active(): dispatch_once(app)
            except Exception:
                logging.exception('策略任务调度失败')
            time.sleep(1)
    threading.Thread(target=loop, daemon=True, name='policy-dispatcher').start()
