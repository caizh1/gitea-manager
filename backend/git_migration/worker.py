"""迁移执行器：全程持有独占锁，中断保留检查点，不冒充完整结果。"""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time

from models import db
from services.upgrade_maintenance import active
from svn_migration.common import MigrationError, encode, file_lock, require
from .models import GitMigrationSource, GitMigrationDiscovery, GitMigrationWorkerHealth, GitMigrationInventory, GitMigrationRun
from .providers import Provider
from .store import root, decrypt
from .transport import Transport
from .inventory import Inventory
from .git_snapshot import GitSnapshot

STOP = False


def heartbeat():
    row = db.session.get(GitMigrationWorkerHealth, 1)
    if row is None:
        row = GitMigrationWorkerHealth(id=1)
        db.session.add(row)
    row.heartbeat_at = datetime.utcnow()
    db.session.commit()


def health():
    row = db.session.get(GitMigrationWorkerHealth, 1)
    return {'online': bool(row and row.heartbeat_at > datetime.utcnow() - timedelta(seconds=90)),
            'heartbeat_at': row.heartbeat_at.isoformat() + 'Z' if row else None}


def recover_interrupted():
    if active():
        return False
    for model in (GitMigrationDiscovery, GitMigrationInventory, GitMigrationRun):
        model.query.filter_by(status='running').update({
            'status': 'interrupted', 'error': '执行器已重启，结果未完成；迁移执行可从检查点继续，发现或盘点需重新发起',
            'error_code': 'interrupted', 'completed_at': datetime.utcnow()}, synchronize_session=False)
    db.session.commit()
    return True


def step():
    if active():
        return False
    heartbeat()
    model = GitMigrationDiscovery
    row = model.query.filter_by(status='queued').order_by(model.id).first()
    if row is None:
        model = GitMigrationInventory
        row = model.query.filter_by(status='queued').order_by(model.id).first()
    if row is None:
        model = GitMigrationRun
        row = model.query.filter_by(status='queued').order_by(model.id).first()
    if row is None:
        return False
    ident = row.id
    changed = model.query.filter_by(id=ident, status='queued').update(
        {'status': 'running', 'started_at': datetime.utcnow()}, synchronize_session=False)
    db.session.commit()
    if changed != 1:
        return False
    last_tick = time.monotonic()

    def tick():
        nonlocal last_tick
        require(not STOP, '执行器停止，任务尚未完成，已保留检查点', 'interrupted')
        require(not active(), '升级维护中，任务已中断，维护完成后可继续核对', 'maintenance')
        if time.monotonic() - last_tick >= 15:
            heartbeat()
            last_tick = time.monotonic()

    transport = None
    try:
        row = db.session.get(model, ident)
        source = db.session.get(GitMigrationSource, row.source_id)
        require(source and not source.archived and source.version == row.source_version,
                '来源配置已变化，请重新发起发现', 'stale')
        with tempfile.TemporaryDirectory(prefix='credentials-', dir=root()) as directory:
            verify = True
            if source.ca_pem:
                path = Path(directory) / 'ca.pem'
                path.write_text(source.ca_pem)
                path.chmod(0o600)
                verify = str(path)
            transport = Transport(source.url, verify=verify, tick=tick,
                                  deadline_seconds=86400 if model is GitMigrationRun else 1800)
            provider = Provider(transport, source.kind, source.username,
                                decrypt(source.secret_ciphertext), source.auth_method)
            if model is GitMigrationRun:
                from .runs import execute
                execute(row, provider, tick)
                return True
            if model is GitMigrationDiscovery:
                result = provider.discover()
            else:
                identity = provider.authenticate()
                discovery = db.session.get(GitMigrationDiscovery, row.discovery_id)
                snapshot = json.loads(discovery.result_json)
                require(identity['actor'] == snapshot['actor'] and identity['version'] == snapshot['version'],
                        '来源身份或版本已变化，请重新发现', 'stale')
                ids = set(json.loads(row.project_ids_json))
                from .dependencies import inspect_closure
                result = {**identity, 'projects': inspect_closure(provider, snapshot['projects'], ids, tick),
                          'requested_project_ids': sorted(ids), 'migration_verified': False}
                total_size = 0
                for item in result['projects']:
                    if row.capture_code:
                        directory = root() / 'code-snapshots' / str(ident) / str(item['project']['id'])
                        item['code_snapshot'] = GitSnapshot(provider, item['project'], directory, tick=tick).capture()
                    total_size += len(encode(item).encode())
                    require(total_size <= 64 * 1024 * 1024, '依赖与代码快照总容量超限，请缩小批次', 'capacity')
            tick()
        db.session.expire_all()
        row = db.session.get(model, ident)
        source = db.session.get(GitMigrationSource, row.source_id)
        require(source.version == row.source_version, '来源配置变化，结果不再有效', 'stale')
        row.result_json, row.status = encode(result), 'succeeded'
        row.error, row.error_code = '', ''
    except MigrationError as error:
        db.session.rollback()
        row = db.session.get(model, ident)
        row.status = ('interrupted' if error.code in ('maintenance', 'interrupted') else
                      'uncertain' if model is GitMigrationRun else 'failed')
        row.error, row.error_code = str(error), error.code
    except Exception:
        db.session.rollback()
        row = db.session.get(model, ident)
        row.status, row.error_code = ('uncertain' if model is GitMigrationRun else 'failed'), 'internal'
        row.error = '执行遇到内部异常，未生成完整结果；已保留现场和检查点，请检查执行环境'
    finally:
        if transport:
            transport.close()
    row.completed_at = datetime.utcnow()
    db.session.commit()
    return True


def main():
    global STOP
    os.umask(0o077)
    from flask import Flask
    from config import DATABASE_URL
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI=DATABASE_URL, SQLALCHEMY_TRACK_MODIFICATIONS=False,
                      SQLALCHEMY_ENGINE_OPTIONS={'connect_args': {'timeout': 30}})
    db.init_app(app)
    with app.app_context():
        if '--check' in sys.argv:
            import subprocess
            subprocess.run(['git', '--version'], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(['git', 'lfs', 'version'], check=True, stdout=subprocess.DEVNULL)
            try:
                with file_lock(root() / 'worker.lock'):
                    raise SystemExit('Git 发现执行器未运行')
            except MigrationError as error:
                if error.code != 'busy':
                    raise
            print('Git 发现执行器锁检查通过')
            return

        def stop(_signum, _frame):
            global STOP
            STOP = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        with file_lock(root() / 'worker.lock'):
            recovered = False
            while not STOP:
                try:
                    if not recovered:
                        if active():
                            time.sleep(1)
                            continue
                        from services.database_startup import database_startup_lock
                        with database_startup_lock(app):
                            db.create_all()
                            from sqlalchemy import inspect
                            if 'capture_code' not in {c['name'] for c in inspect(db.engine).get_columns('git_migration_inventories')}:
                                db.session.execute(db.text('ALTER TABLE git_migration_inventories ADD COLUMN capture_code BOOLEAN NOT NULL DEFAULT 0'))
                                db.session.commit()
                        from svn_migration.engine import reap_processes
                        reap_processes(root())
                        recovered = recover_interrupted()
                    if not step():
                        time.sleep(1)
                finally:
                    db.session.remove()


if __name__ == '__main__':
    main()
