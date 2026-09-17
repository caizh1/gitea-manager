"""独立 SQLite 队列；短事务、执行代次与租约共同隔离过期任务。"""
import contextlib
import fcntl
import os
import secrets
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .common import ReviewError, Stale, encode, secret_file
import json

DEFAULTS = {'enabled': False, 'engine': 'opencode_cli', 'api_base_url': '', 'model': '',
            'api_key': '', 'revision': 1, 'debounce_seconds': 20, 'max_files': 40,
            'max_calls': 32, 'task_timeout_seconds': 1200, 'max_findings': 20}
ACTIVE = ('queued', 'running', 'publishing', 'recovering')


class Store:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get('AI_REVIEW_DB', '/app/review/review.db'))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock('初始化', blocking=True), contextlib.closing(self.connect()) as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS configuration_history (revision INTEGER PRIMARY KEY, value TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS repositories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER NOT NULL,
                repo_id INTEGER NOT NULL, value TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1, last_event REAL, last_reconcile REAL DEFAULT 0,
                UNIQUE(server_id,repo_id));
            CREATE TABLE IF NOT EXISTS deliveries (
                repository INTEGER NOT NULL, delivery TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(repository,delivery));
            CREATE TABLE IF NOT EXISTS pr_state (
                repository INTEGER NOT NULL, number INTEGER NOT NULL, generation INTEGER NOT NULL,
                signature TEXT NOT NULL, summary_id INTEGER, PRIMARY KEY(repository,number));
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, repository INTEGER NOT NULL, number INTEGER NOT NULL,
                generation INTEGER NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL, available REAL NOT NULL, updated REAL NOT NULL,
                config_revision INTEGER NOT NULL, repo_revision INTEGER NOT NULL,
                owner TEXT NOT NULL DEFAULT '', lease REAL NOT NULL DEFAULT 0,
                snapshot TEXT, result TEXT, review_id INTEGER, error TEXT NOT NULL DEFAULT '',
                manual INTEGER NOT NULL DEFAULT 0, model_calls INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,available);
            CREATE INDEX IF NOT EXISTS jobs_pr ON jobs(repository,number,created);
            CREATE TABLE IF NOT EXISTS capabilities (
                token_hash TEXT PRIMARY KEY, job TEXT NOT NULL, owner TEXT NOT NULL,
                expires REAL NOT NULL, remaining INTEGER NOT NULL, model TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS health (name TEXT PRIMARY KEY, updated REAL NOT NULL, detail TEXT NOT NULL);
            ''')
            c.execute('INSERT OR IGNORE INTO settings VALUES (1,?)', (encode(DEFAULTS),))
            if 'model_calls' not in {r[1] for r in c.execute('PRAGMA table_info(jobs)')}:
                c.execute('ALTER TABLE jobs ADD COLUMN model_calls INTEGER NOT NULL DEFAULT 0')
            if 'attempts' not in {r[1] for r in c.execute('PRAGMA table_info(jobs)')}:
                c.execute('ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0')
            if 'signature' not in {r[1] for r in c.execute('PRAGMA table_info(deliveries)')}:
                c.execute("ALTER TABLE deliveries ADD COLUMN signature TEXT NOT NULL DEFAULT ''")
            from .progress_state import migrate
            migrate(c)
            from .attention import migrate as migrate_attention
            migrate_attention(c)
            from .dataset import migrate as migrate_dataset
            migrate_dataset(c)
            current = self.config(public=True, connection=c)
            c.execute('INSERT OR IGNORE INTO configuration_history VALUES (?,?,?)', (current['revision'],encode(current),time.time()))
            c.commit()
        os.chmod(self.path, 0o600)

    def connect(self):
        c = sqlite3.connect(self.path, timeout=1.0)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        return c

    def validate_storage(self):
        """独立进程验证存储锁；不支持锁的共享目录不能启动发布工作进程。"""
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory(prefix='锁验证-',dir=self.path.parent) as folder:
            lock_path=Path(folder)/'互斥.lock'
            with lock_path.open('a') as handle:
                fcntl.flock(handle,fcntl.LOCK_EX)
                probe='import fcntl,sys\nf=open(sys.argv[1],"a")\ntry:\n fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept BlockingIOError:\n sys.exit(0)\nsys.exit(1)'
                result=subprocess.run([sys.executable,'-c',probe,str(lock_path)],capture_output=True,timeout=10)
                if result.returncode:raise ReviewError('Review 数据目录不支持可靠进程锁，请使用 Linux 原生持久卷')
            database=Path(folder)/'事务.db'
            with contextlib.closing(sqlite3.connect(database)) as c:
                c.execute('CREATE TABLE probe(id INTEGER)');c.commit();c.execute('BEGIN IMMEDIATE')
                probe='import sqlite3,sys\nc=sqlite3.connect(sys.argv[1],timeout=0.1)\ntry:\n c.execute("BEGIN IMMEDIATE")\nexcept sqlite3.OperationalError:\n sys.exit(0)\nsys.exit(1)'
                result=subprocess.run([sys.executable,'-c',probe,str(database)],capture_output=True,timeout=10)
                if result.returncode:raise ReviewError('Review 数据目录不支持可靠 SQLite 事务锁')

    @contextlib.contextmanager
    def tx(self):
        c = self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    @contextlib.contextmanager
    def lock(self, name, blocking=False, shared=False):
        with (self.path.parent / (name + '.lock')).open('a') as f:
            try:
                fcntl.flock(f, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                raise ReviewError('该 PR 正在由其他执行者发布') from None
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def seal(self, value):
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ReviewError('凭据格式无效')
        try:
            return Fernet(secret_file('AI_REVIEW_MASTER_KEY').encode()).encrypt(value.encode()).decode()
        except ValueError:
            raise ReviewError('AI Review 主密钥格式无效') from None

    def unseal(self, value):
        if not value:
            raise ReviewError('凭据尚未配置')
        try:
            return Fernet(secret_file('AI_REVIEW_MASTER_KEY').encode()).decrypt(value.encode()).decode()
        except (ValueError, InvalidToken):
            raise ReviewError('凭据无法解密，请检查部署主密钥') from None

    def config(self, public=False, connection=None):
        if connection:
            result = json.loads(connection.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
        else:
            with contextlib.closing(self.connect()) as c:
                result = self.config(connection=c)
        if public:
            result['api_key_configured'] = bool(result.pop('api_key', ''))
        return result

    def save_config(self, data):
        with self.tx() as c:
            old = self.config(connection=c)
            if data.pop('revision', None) != old['revision']:
                raise ReviewError('配置已变化，请刷新后保存')
            old.update(data); old['revision'] += 1
            c.execute('UPDATE settings SET value=? WHERE id=1', (encode(old),))
            public = dict(old); public['api_key_configured'] = bool(public.pop('api_key',''))
            c.execute('INSERT INTO configuration_history VALUES (?,?,?)', (public['revision'],encode(public),time.time()))
            c.execute("UPDATE jobs SET status='superseded',error='配置已变更',updated=? WHERE status IN ('queued','running')", (time.time(),))
            c.execute('UPDATE repositories SET last_reconcile=0')
            c.execute('UPDATE pr_state SET progress_seq=progress_seq+1')
        return self.config(public=True)

    def repo(self, repository, connection=None, public=False):
        if connection:
            row = connection.execute('SELECT * FROM repositories WHERE id=?', (repository,)).fetchone()
            if not row:
                raise ReviewError('未配置该 Review 仓库')
            result = dict(row); result.update(json.loads(result.pop('value')))
        else:
            with contextlib.closing(self.connect()) as c:
                result = self.repo(repository, c)
        if public:
            for key in ('bot_token', 'webhook_secret'):
                result[key + '_configured'] = bool(result.pop(key, ''))
        return result

    def repos(self, public=False):
        with contextlib.closing(self.connect()) as c:
            return [self.repo(row[0], c, public) for row in c.execute('SELECT id FROM repositories ORDER BY id')]

    def save_repo(self, server_id, repo_id, data, repository=None):
        with self.tx() as c:
            if repository:
                old = self.repo(repository, c)
                if data.pop('revision', None) != old['revision']:
                    raise ReviewError('仓库配置已变化，请刷新')
                protected = ('id', 'server_id', 'repo_id', 'revision', 'last_event', 'last_reconcile')
                value = {k: v for k, v in old.items() if k not in protected}
                value.update(data)
                c.execute('UPDATE repositories SET value=?,enabled=?,revision=revision+1,last_reconcile=0 WHERE id=?',
                          (encode(value), bool(value['enabled']), repository))
                c.execute("UPDATE jobs SET status='superseded',error='仓库配置已变更',updated=? WHERE repository=? AND status IN ('queued','running')",
                          (time.time(), repository))
                c.execute('UPDATE pr_state SET progress_seq=progress_seq+1 WHERE repository=?', (repository,))
            else:
                try:
                    cur = c.execute('INSERT INTO repositories(server_id,repo_id,value,enabled) VALUES (?,?,?,?)',
                                    (server_id, repo_id, encode(data), bool(data.get('enabled'))))
                except sqlite3.IntegrityError:
                    raise ReviewError('该仓库已配置，请编辑现有配置') from None
                repository = cur.lastrowid
        return self.repo(repository, public=True)

    def repo_status(self, repository, **values):
        with self.tx() as c:
            row = c.execute('SELECT value FROM repositories WHERE id=?', (repository,)).fetchone()
            if not row:
                return
            value = json.loads(row[0]); value.update(values)
            c.execute('UPDATE repositories SET value=? WHERE id=?', (encode(value), repository))

    def enqueue(self, repository, number, signature, delivery=None, closed=False, manual=False):
        now = time.time()
        with self.tx() as c:
            repo, config = self.repo(repository, c), self.config(connection=c)
            if delivery:
                inserted = c.execute('INSERT OR IGNORE INTO deliveries(repository,delivery,created,signature) VALUES (?,?,?,?)',
                                     (repository, delivery, now, encode({'version':signature,'closed':closed}))).rowcount
                if not inserted:
                    return None
                c.execute('UPDATE repositories SET last_event=? WHERE id=?', (now, repository))
            if not config['enabled'] or not repo['enabled']:
                return None
            state = c.execute('SELECT * FROM pr_state WHERE repository=? AND number=?', (repository, number)).fetchone()
            target_head = signature.get('head', '') if isinstance(signature, dict) else ''
            signature = encode([signature, config['revision'], repo['revision'], bool(closed)])
            if state and state['signature'] == signature and not manual:
                return None
            generation = (state['generation'] if state else 0) + 1
            c.execute('''INSERT INTO pr_state(repository,number,generation,signature) VALUES (?,?,?,?)
                       ON CONFLICT(repository,number) DO UPDATE SET generation=excluded.generation,signature=excluded.signature''',
                      (repository, number, generation, signature))
            c.execute("UPDATE jobs SET status='superseded',error='PR 已更新或关闭',updated=? WHERE repository=? AND number=? AND status IN ('queued','running')", (now, repository, number))
            if closed:
                return None
            job = secrets.token_hex(16)
            c.execute('''INSERT INTO jobs(id,repository,number,generation,status,stage,created,available,updated,config_revision,repo_revision,manual,target_head)
                         VALUES (?,?,?,?,'queued','等待执行',?,?,?,?,?,?,?)''',
                      (job, repository, number, generation, now, now + (0 if manual else config['debounce_seconds']), now,
                       config['revision'], repo['revision'], int(manual), target_head))
            return job

    def job(self, job, public=False):
        with contextlib.closing(self.connect()) as c:
            row = c.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
            cfg = c.execute('SELECT value FROM configuration_history WHERE revision=?', (row['config_revision'],)).fetchone() if row else None
            sync = c.execute('SELECT progress_error,progress_sent_at FROM pr_state WHERE repository=? AND number=?', (row['repository'], row['number'])).fetchone() if row else None
            from .attention import blocking, worker_state
            attention = blocking(c, row['repository'], row['number']) if row else None
            worker = worker_state(c)
        if not row:
            raise ReviewError('任务不存在')
        value = dict(row)
        value['attention'] = attention
        from .attention import reconciliation
        value['reconciliation'] = reconciliation(value, worker)
        if value['status'] == 'superseded':
            value['stage'] = '版本已过期'
        value['configuration'] = json.loads(cfg[0]) if cfg else None
        for key in ('snapshot', 'result'):
            value[key] = json.loads(value[key]) if value[key] else None
        from .progress_state import job_progress
        value['progress'] = job_progress(value)
        value['progress']['sync_error'] = sync['progress_error'] if sync else ''
        value['progress']['synced_at'] = sync['progress_sent_at'] if sync else None
        if public:
            value.pop('owner', None)
            if value['snapshot']:
                value['snapshot'] = {k: value['snapshot'][k] for k in ('id', 'base', 'head', 'merge_base', 'coverage', 'analysis_version') if k in value['snapshot']}
        return value

    def jobs(self, page=1, repository=None, number=None, status=None):
        from .attention import STATES
        if status is not None and status not in STATES:
            raise ReviewError('不支持的任务状态')
        conditions, args = [], []
        for field, value in (('repository', repository), ('number', number), ('status', status)):
            if value is not None:
                conditions.append(field+'=?'); args.append(value)
        where = ' WHERE '+' AND '.join(conditions) if conditions else ''
        with contextlib.closing(self.connect()) as c:
            total = c.execute('SELECT count(*) FROM jobs' + where, args).fetchone()[0]
            ids = [r[0] for r in c.execute('SELECT id FROM jobs' + where + ' ORDER BY created DESC LIMIT 20 OFFSET ?', args + [(page-1)*20])]
        values = [self.job(i, public=True) for i in ids]
        for v in values:
            v.pop('result', None)
        return {'items': values, 'total': total}

    def claim(self):
        now, owner = time.time(), secrets.token_hex(16)
        with self.tx() as c:
            # 发布中的请求可能已经生效，中断后只能进入核对流程。
            c.execute("UPDATE jobs SET status='publish_unknown',owner='',updated=?,error='执行中断，等待核对实际发布结果' WHERE status IN ('publishing','recovering') AND lease<?", (now, now))
            c.execute("UPDATE jobs SET status='failed',stage='中断次数达到上限',error='已中断三次，请排查环境后人工重审',updated=? WHERE status='running' AND lease<? AND attempts>=3", (now, now))
            c.execute("UPDATE jobs SET status='queued',owner='',available=?,updated=?,stage='中断后重新领取' WHERE status='running' AND lease<?", (now, now, now))
            if not self.config(connection=c)['enabled']:
                return None
            if c.execute("SELECT 1 FROM jobs WHERE status IN ('running','publishing','recovering') AND lease>=? LIMIT 1", (now,)).fetchone():
                return None
            row = c.execute("""SELECT j.id FROM jobs j WHERE j.status='queued' AND j.available<=?
                               AND NOT EXISTS (SELECT 1 FROM jobs x WHERE x.repository=j.repository AND x.number=j.number
                                               AND x.status='publish_unknown') ORDER BY j.created LIMIT 1""", (now,)).fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET status='running',stage='读取固定版本',units_completed=0,units_total=NULL,owner=?,lease=?,updated=?,attempts=attempts+1 WHERE id=?", (owner, now+90, now, row[0]))
        return self.job(row[0])

    def _active(self, c, job, owner):
        row = c.execute('''SELECT j.*,r.enabled,r.revision AS current_repo_revision,p.generation AS current_generation
                           FROM jobs j JOIN repositories r ON r.id=j.repository
                           JOIN pr_state p ON p.repository=j.repository AND p.number=j.number WHERE j.id=?''', (job,)).fetchone()
        cfg = self.config(connection=c)
        if (not row or row['owner'] != owner or row['status'] not in ('running', 'publishing', 'recovering')
                or row['lease'] < time.time() or row['generation'] != row['current_generation']
                or not row['enabled'] or not cfg['enabled'] or row['config_revision'] != cfg['revision']
                or row['repo_revision'] != row['current_repo_revision']):
            raise Stale('任务版本、配置或执行租约已过期')
        return row

    def active(self, job, owner):
        with contextlib.closing(self.connect()) as c:
            return dict(self._active(c, job, owner))

    def update(self, job, owner, **fields):
        allowed = {'status', 'stage', 'snapshot', 'result', 'review_id', 'error',
                   'progress_stage', 'units_completed', 'units_total', 'target_head', 'review_fragment'}
        if not fields or set(fields) - allowed:
            raise ValueError('不支持的任务字段')
        with self.tx() as c:
            self._active(c, job, owner)
            values = [encode(v) if k in ('snapshot', 'result') else v for k, v in fields.items()]
            c.execute('UPDATE jobs SET ' + ','.join(k+'=?' for k in fields) + ',updated=? WHERE id=? AND owner=?', values+[time.time(), job, owner])

    def finish(self, job, owner, status, error=''):
        with self.tx() as c:
            # 只改变本执行者仍持有的行；已被新事件取消的任务不得复活。
            c.execute("UPDATE jobs SET status=?,stage=?,error=?,updated=?,lease=0 WHERE id=? AND owner=? AND status IN ('running','publishing','recovering')",
                      (status, {'succeeded': '已完成', 'superseded': '版本已过期', 'failed': '执行失败',
                                'publish_unknown': '发布结果待核对'}.get(status, status), error[:1000], time.time(), job, owner))
            c.execute('DELETE FROM capabilities WHERE job=?', (job,))

    def heartbeat(self, job, owner):
        with self.tx() as c:
            self._active(c, job, owner)
            c.execute('UPDATE jobs SET lease=? WHERE id=? AND owner=?', (time.time()+90, job, owner))
            c.execute('''UPDATE pr_state SET progress_seq=progress_seq+1 WHERE progress_sent_at<=?
                         AND (repository,number,generation) IN
                         (SELECT repository,number,generation FROM jobs WHERE id=?)''', (time.time()-30, job))

    def health(self, name, detail):
        with self.tx() as c:
            c.execute('INSERT INTO health VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET updated=excluded.updated,detail=excluded.detail', (name, time.time(), detail))

    def health_status(self):
        with contextlib.closing(self.connect()) as c:
            return [dict(r) for r in c.execute('SELECT * FROM health')]

    def summary_id(self, repository, number, value=None):
        with self.tx() as c:
            if value is not None:
                c.execute('UPDATE pr_state SET summary_id=? WHERE repository=? AND number=?', (value, repository, number))
            row = c.execute('SELECT summary_id FROM pr_state WHERE repository=? AND number=?', (repository, number)).fetchone()
            return row[0] if row else None

    def prune(self):
        with self.tx() as c:
            cutoff = time.time() - 30*86400
            c.execute('DELETE FROM deliveries WHERE created<?', (cutoff,))
            c.execute('DELETE FROM capabilities WHERE expires<?', (time.time(),))
            # 保留发布歧义证据，普通已结束任务在 30 天后释放源码快照。
            c.execute("UPDATE jobs SET snapshot=NULL,result=NULL WHERE updated<? AND (status IN ('failed','superseded') OR (status='succeeded' AND id IN (SELECT job FROM review_archive_jobs)))", (cutoff,))
            c.execute("DELETE FROM review_feedback_events WHERE status='done' AND due<?", (cutoff,))
