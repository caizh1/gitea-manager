"""迁移与现有破坏性实例操作双向互斥，锁与 SQLite 同卷。"""
import contextlib
from datetime import datetime
import functools
import time

from flask import g, request, jsonify
from models import db, Backup, RestoreTask
from .common import data_root, file_lock, MigrationError
from .models import SvnMigrationRun


@contextlib.contextmanager
def target_guard(server_id, migration=False):
    if not migration and db.engine.url.database == ':memory:':
        yield
        return
    with file_lock(data_root() / '.instance-operations' / (str(server_id) + '.lock')):
        if migration:
            busy = Backup.query.filter(Backup.source_server_id == server_id,
                (Backup.status == 'running') | Backup.recovery_pending.is_(True)).first() or RestoreTask.query.filter_by(target_server_id=server_id, status='running').first()
            if busy:
                raise MigrationError('目标正在备份或恢复，迁移暂缓执行', 'busy', 409)
        else:
            if SvnMigrationRun.query.filter(SvnMigrationRun.server_id == server_id, SvnMigrationRun.status.in_(('running', 'paused', 'uncertain'))).first():
                raise MigrationError('该实例有执行中、暂停或待确认的 SVN 迁移，请先处理迁移任务', 'busy', 409)
        yield


def guard_task(model, field):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(task_id, *args, **kwargs):
            task = db.session.get(model, task_id)
            if task is None:
                return function(task_id, *args, **kwargs)
            try:
                # 创建任务的 HTTP 请求可能还在释放同一个短锁；只等待这个短窗口。
                stack = contextlib.ExitStack()
                for attempt in range(20):
                    try:
                        stack.enter_context(target_guard(getattr(task, field)))
                        break
                    except MigrationError:
                        if attempt == 19:
                            raise
                        time.sleep(0.05)
                with stack:
                    return function(task_id, *args, **kwargs)
            except MigrationError as error:
                db.session.rollback()
                task = db.session.get(model, task_id)
                task.status, task.error_msg, task.completed_at = 'failed', str(error), datetime.utcnow()
                task.progress_stage, task.progress_label = 'failed', '实例操作互斥'
                db.session.commit()
        return wrapped
    return decorate


def install(app):
    @app.before_request
    def protect_instance():
        endpoint = request.endpoint
        # 全局钩子只处理实际互斥的接口，不能预先消耗 Webhook 等接口的原始正文。
        if endpoint not in ('servers.update_server', 'servers.delete_server',
                            'backups.create_backup', 'restore.execute_restore', 'restore.recover_restore'):
            return None
        ids = []
        if endpoint in ('servers.update_server', 'servers.delete_server'):
            ids = [request.view_args['sid']]
        elif endpoint == 'backups.create_backup':
            data = request.get_json(silent=True) or {}
            ids = [data.get('source_server_id')]
        elif endpoint == 'restore.execute_restore':
            data = request.get_json(silent=True) or {}
            ids = [data.get('target_server_id')]
        elif endpoint == 'restore.recover_restore':
            task = db.session.get(RestoreTask, request.view_args['task_id'])
            ids = [task.target_server_id] if task else []
        if ids:
            stack = contextlib.ExitStack()
            try:
                for ident in sorted(set(ids) - {None}):
                    stack.enter_context(target_guard(ident))
                g.svn_instance_guard = stack
            except MigrationError as error:
                stack.close()
                return jsonify(error=str(error), code=error.code), error.status

    @app.teardown_request
    def release_instance(_error):
        stack = g.pop('svn_instance_guard', None)
        if stack:
            stack.close()
