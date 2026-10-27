"""独立 SQLite 队列；短事务、执行代次与租约共同隔离过期任务。"""
import contextlib
import fcntl
import os
import secrets
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .token_budget import DEFAULTS as BATCH_DEFAULTS
from .thinking import DEFAULTS as THINKING_DEFAULTS
from .common import ReviewError, Stale, encode, secret_file
from .budgets import TIME_FIELDS, CONTEXT_DEFAULTS, MODEL_TIMEOUT_DEFAULT, REVIEW_TIMEOUT_DEFAULT, execution_revision
import json

DEFAULTS = {**BATCH_DEFAULTS, **THINKING_DEFAULTS, **CONTEXT_DEFAULTS, 'enabled': False, 'engine': 'opencode_native', 'api_base_url': '', 'model': '',
            'api_key': '', 'revision': 1, 'debounce_seconds': 20, 'max_files': 40,
            'max_calls': 32, 'task_timeout_seconds': 1200, 'review_timeout_seconds': REVIEW_TIMEOUT_DEFAULT,
            'model_request_timeout_seconds': MODEL_TIMEOUT_DEFAULT,
            'execution_revision': 1, 'model_max_output_tokens': 32768, 'model_response_max_mib': 8, 'max_findings': 20}
ACTIVE = ('queued', 'running', 'publishing', 'recovering')


class Store:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get('AI_REVIEW_DB', '/app/review/review.db'))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self.connect()) as c:
            if c.execute('PRAGMA user_version').fetchone()[0] >= 17:
                return
        with self.lock('初始化', blocking=True), contextlib.closing(self.connect()) as c:
            if c.execute('PRAGMA user_version').fetchone()[0] >= 17:
                return
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
            from .dataset import migrate as migrate_dataset
            migrate_dataset(c)
            current = self.config(public=True, connection=c)
            c.execute('INSERT OR IGNORE INTO configuration_history VALUES (?,?,?)', (current['revision'],encode(current),time.time()))
            from .review_state import migrate
            migrate(c)
            from .repository_context import migrate
            migrate(c)
            from .performance import migrate
            migrate(c)
            from .session_events import migrate as migrate_session_events
            migrate_session_events(c)
            columns = {r[1] for r in c.execute('PRAGMA table_info(jobs)')}
            for column in ('access_scope', 'access_actor'):
                if column not in columns:
                    c.execute('ALTER TABLE jobs ADD COLUMN ' + column + " TEXT NOT NULL DEFAULT '{}'")
            from .attention import migrate as migrate_attention
            migrate_attention(c)
            from .cancellation import migrate as migrate_cancellation
            migrate_cancellation(c)
            from .issue_lifecycle import migrate as migrate_issues
            migrate_issues(c)
            c.execute('CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created,id)')
            c.execute('CREATE INDEX IF NOT EXISTS jobs_repository_created ON jobs(repository,created,id)')
            c.execute('CREATE INDEX IF NOT EXISTS review_issues_origin ON review_issues(origin_job)')
            c.execute("CREATE INDEX IF NOT EXISTS finding_archive_job ON review_finding_archive(json_extract(value,'$.job'))")
            from .batch_store import migrate as migrate_batches
            migrate_batches(c)
            c.execute('PRAGMA user_version=17')
            c.commit()
        os.chmod(self.path, 0o600)

    def connect(self):
        c = sqlite3.connect(self.path, timeout=5.0)
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

    def seal_evidence(self, value, limit_bytes):
        """原始失败证据单独限额，不能套用 4096 字符的凭据格式校验。"""
        raw=value.encode('utf-8')
        if len(raw)>limit_bytes:raise ReviewError(f'失败证据 {len(raw)} 字节超过 {limit_bytes} 字节存储保护')
        return Fernet(secret_file('AI_REVIEW_MASTER_KEY').encode()).encrypt(raw).decode()

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
        result.setdefault('model_request_timeout_seconds', MODEL_TIMEOUT_DEFAULT)
        result.setdefault('review_timeout_seconds', result.get('task_timeout_seconds', REVIEW_TIMEOUT_DEFAULT))
        result.setdefault('model_max_output_tokens', 0)
        # 新安装已完整写入 DEFAULTS；旧配置缺字段时保留原来的隐含预算。
        result.setdefault('batch_input_tokens', 32768)
        result.setdefault('model_response_max_mib', 8)
        result.setdefault('execution_revision', result['revision'])
        if 'thinking_protocol' not in result and result.get('model', '').lower().startswith('deepseek'):
            result['thinking_protocol'] = 'deepseek'
        for key, default in THINKING_DEFAULTS.items():
            result.setdefault(key, default)
        for key, default in dict(CONTEXT_DEFAULTS, **BATCH_DEFAULTS).items():
            result.setdefault(key, default)
        if public:
            result.pop('execution_revision', None)
            result['api_key_configured'] = bool(result.pop('api_key', ''))
        return result

    def historical_config(self, revision, connection):
        row = connection.execute('SELECT value FROM configuration_history WHERE revision=?', (revision,)).fetchone()
        if not row:
            raise ReviewError('任务配置历史缺失，不能使用当前预算替代')
        return json.loads(row[0])

    def job_config(self, job_id, connection=None):
        if connection is None:
            with contextlib.closing(self.connect()) as c:
                return self.job_config(job_id, c)
        row = connection.execute('SELECT config_revision FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ReviewError('任务不存在')
        saved = self.historical_config(row['config_revision'], connection)
        current = self.config(connection=connection)
        if execution_revision(saved) != execution_revision(current):
            raise Stale('任务模型配置已过期')
        # 密钥只从仍匹配执行版本的当前配置取出，预算从不可变历史读取。
        result = dict(saved, api_key=current['api_key'])
        result.pop('api_key_configured', None)
        result.setdefault('model_request_timeout_seconds', MODEL_TIMEOUT_DEFAULT)
        result.setdefault('review_timeout_seconds', result.get('task_timeout_seconds', REVIEW_TIMEOUT_DEFAULT))
        result.setdefault('model_max_output_tokens', 0)
        result.setdefault('model_response_max_mib', 8)
        result.setdefault('execution_revision', saved['revision'])
        return result

    def execution_revision_for(self, revision, connection=None):
        if connection is None:
            with contextlib.closing(self.connect()) as c:
                return self.execution_revision_for(revision, c)
        row = connection.execute('SELECT value FROM configuration_history WHERE revision=?', (revision,)).fetchone()
        # 缺失的旧历史只用于保留旧版本身份；job_config 仍拒绝执行它。
        return execution_revision(json.loads(row[0])) if row else revision

    def save_config(self, data):
        data = dict(data)
        with self.tx() as c:
            old = self.config(connection=c)
            if data.pop('revision', None) != old['revision']:
                raise ReviewError('配置已变化，请刷新后保存')
            data.pop('execution_revision', None)
            changes = {key for key, value in data.items() if old.get(key) != value}
            invalidate = bool(changes - TIME_FIELDS)
            old.update(data); old['revision'] += 1
            if invalidate:
                old['execution_revision'] = old['revision']
            c.execute('UPDATE settings SET value=? WHERE id=1', (encode(old),))
            public = dict(old); public['api_key_configured'] = bool(public.pop('api_key',''))
            c.execute('INSERT INTO configuration_history VALUES (?,?,?)', (public['revision'],encode(public),time.time()))
            if invalidate:
                c.execute("UPDATE jobs SET status='superseded',error='配置已变更',updated=? WHERE status IN ('queued','running','awaiting_cost','budget_paused','incomplete')", (time.time(),))
                c.execute('UPDATE repositories SET last_reconcile=0')
                c.execute('UPDATE pr_state SET progress_seq=progress_seq+1')
        return self.config(public=True)

    def repo(self, repository, connection=None, public=False):
        if connection:
            row = connection.execute('SELECT * FROM repositories WHERE id=?', (repository,)).fetchone()
            if not row:
                raise ReviewError('未配置该 Review 仓库')
            result = dict(row); result.update(json.loads(result.pop('value')))
            if result.get('access_archived'):
                result['repo_id'] = result.get('original_repo_id', result['repo_id'])
        else:
            with contextlib.closing(self.connect()) as c:
                result = self.repo(repository, c)
        if result.get('gitea_url'):
            from .host_policy import gitea_address
            result['gitea_url'] = gitea_address(result['gitea_url'])
        if public:
            result.pop('webhook_binding', None)
            for key in ('bot_token', 'webhook_secret'):
                result[key + '_configured'] = bool(result.pop(key, ''))
        return result

    def repos(self, public=False):
        with contextlib.closing(self.connect()) as c:
            return [self.repo(row[0], c, public) for row in c.execute('SELECT id FROM repositories ORDER BY id')]

    def save_repo(self, server_id, repo_id, data, repository=None, required_config_revision=None):
        data = dict(data)
        # 创建回执仅由安装流程写入，客户端不能伪造或清空它。
        data.pop('webhook_binding', None)
        with self.tx() as c:
            if required_config_revision is not None and self.config(connection=c)['revision'] != required_config_revision:
                raise ReviewError('模型配置在接入期间发生变化，未自动启用仓库，请继续检查')
            if repository:
                old = self.repo(repository, c)
                if old.get('access_archived'):
                    raise ReviewError('原仓库配置已封存；请按当前归属重新接入，不会继承原任务和样本')
                if data.pop('revision', None) != old['revision']:
                    raise ReviewError('仓库配置已变化，请刷新')
                protected = ('id', 'server_id', 'repo_id', 'revision', 'last_event', 'last_reconcile')
                value = {k: v for k, v in old.items() if k not in protected}
                value.update(data)
                c.execute('UPDATE repositories SET value=?,enabled=?,revision=revision+1,last_reconcile=0 WHERE id=?',
                          (encode(value), bool(value['enabled']), repository))
                c.execute("UPDATE jobs SET status='superseded',error='仓库配置已变更',updated=? WHERE repository=? AND status IN ('queued','running','awaiting_cost','budget_paused','incomplete')",
                          (time.time(), repository))
                c.execute('UPDATE pr_state SET progress_seq=progress_seq+1 WHERE repository=?', (repository,))
            else:
                previous = c.execute('SELECT id,value FROM repositories WHERE server_id=? AND repo_id=?',
                                     (server_id, repo_id)).fetchone()
                if previous and json.loads(previous['value']).get('access_archived'):
                    # 旧记录仍由原内部主键关联历史，腾出当前仓库唯一键供明确的新接入。
                    c.execute('UPDATE repositories SET repo_id=? WHERE id=?', (-previous['id'], previous['id']))
                try:
                    cur = c.execute('INSERT INTO repositories(server_id,repo_id,value,enabled) VALUES (?,?,?,?)',
                                    (server_id, repo_id, encode(data), bool(data.get('enabled'))))
                except sqlite3.IntegrityError:
                    raise ReviewError('该仓库已配置，请编辑现有配置') from None
                repository = cur.lastrowid
        return self.repo(repository, public=True)

    def archive_repo(self, repository):
        """保留原身份、任务及样本，暂停原组织服务；新接入取得新的内部主键。"""
        with self.tx() as c:
            old = self.repo(repository, c)
            if old.get('access_archived'):
                return
            value = {k: v for k, v in old.items() if k not in ('id', 'server_id', 'repo_id', 'revision', 'last_event', 'last_reconcile')}
            value.update(access_archived=True, original_repo_id=old['repo_id'], enabled=False)
            c.execute('UPDATE repositories SET enabled=0,value=? WHERE id=?', (encode(value), repository))
            c.execute("UPDATE jobs SET status=CASE WHEN status IN ('publishing','recovering') THEN 'publish_unknown' ELSE 'superseded' END, error='原组织归属已失效，任务已封存', updated=? WHERE repository=? AND status IN ('queued','running','publishing','recovering')", (time.time(), repository))

    def repo_status(self, repository, **values):
        with self.tx() as c:
            row = c.execute('SELECT value FROM repositories WHERE id=?', (repository,)).fetchone()
            if not row:
                return
            value = json.loads(row[0]); value.update(values)
            c.execute('UPDATE repositories SET value=? WHERE id=?', (encode(value), repository))

    def enqueue(self, repository, number, signature, delivery=None, closed=False, manual=False, access_actor=None):
        from .runtime_status import available
        if not available() and not closed:
            if manual: raise ReviewError('AI 执行器不可用，请查看安装与运行诊断')
            return None
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
            if state and not manual and not closed:
                current_version = json.loads(state['signature'])[0]
                cancelled = c.execute("SELECT 1 FROM jobs WHERE repository=? AND number=? AND generation=? AND cancel_requested_at IS NOT NULL LIMIT 1",
                                      (repository, number, state['generation'])).fetchone()
                if cancelled and current_version == signature:
                    # 人工取消绑定代码版本；模型/凭据/开关变化不能悄悄重启这个版本。
                    return None
            target_head = signature.get('head', '') if isinstance(signature, dict) else ''
            signature = encode([signature, execution_revision(config), repo['revision'], bool(closed)])
            if state and state['signature'] == signature and not manual:
                return None
            if state and not manual and not closed:
                previous = json.loads(state['signature'])[0]
                if isinstance(previous, dict) and set(previous) == {'manual'}:
                    # 兼容旧版已经结束的人工任务：旧代码只保存快照，未绑定真实版本。
                    # 仅核对本代及当时配置，不能把旧失败当成升级后的新修改自动重跑。
                    legacy = c.execute("""SELECT config_revision,repo_revision,
                        json_extract(snapshot,'$.head') AS head,json_extract(snapshot,'$.base') AS base
                        FROM jobs WHERE repository=? AND number=? AND generation=? AND manual=1
                        AND status IN ('failed','publish_unknown','succeeded') AND snapshot IS NOT NULL
                        LIMIT 1""", (repository, number, state['generation'])).fetchone()
                    if legacy and legacy['head'] and legacy['base']:
                        actual = encode([{'head':legacy['head'], 'base':legacy['base']},
                                         self.execution_revision_for(legacy['config_revision'], c), legacy['repo_revision'], False])
                        if signature == actual:
                            c.execute('UPDATE pr_state SET signature=? WHERE repository=? AND number=? AND generation=?',
                                      (actual, repository, number, state['generation']))
                            return None
            generation = (state['generation'] if state else 0) + 1
            c.execute('''INSERT INTO pr_state(repository,number,generation,signature) VALUES (?,?,?,?)
                       ON CONFLICT(repository,number) DO UPDATE SET generation=excluded.generation,signature=excluded.signature''',
                      (repository, number, generation, signature))
            c.execute("UPDATE jobs SET status='superseded',error='PR 已更新或关闭',updated=? WHERE repository=? AND number=? AND status IN ('queued','running','awaiting_cost','budget_paused','incomplete')", (now, repository, number))
            if closed:
                return None
            job = secrets.token_hex(16)
            c.execute('''INSERT INTO jobs(id,repository,number,generation,status,stage,created,available,updated,config_revision,repo_revision,manual,target_head,batch_protocol)
                         VALUES (?,?,?,?,'queued','等待执行',?,?,?,?,?,?,?,1)''',
                      (job, repository, number, generation, now, now + (0 if manual else config['debounce_seconds']), now,
                       config['revision'], repo['revision'], int(manual), target_head))
            c.execute('UPDATE jobs SET access_scope=?,access_actor=? WHERE id=?',
                      (encode(repo.get('access_scope') or {}), encode(access_actor or {}), job))
            return job

    def job(self, job, public=False, evidence=True):
        with contextlib.closing(self.connect()) as c:
            row = c.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
            cfg = c.execute('SELECT value FROM configuration_history WHERE revision=?', (row['config_revision'],)).fetchone() if row else None
            sync = c.execute('SELECT progress_error,progress_sent_at FROM pr_state WHERE repository=? AND number=? AND generation=?', (row['repository'], row['number'], row['generation'])).fetchone() if row else None
        if not row:
            raise ReviewError('任务不存在')
        value = dict(row)
        from .attention import blocking, worker_state, reconciliation
        with contextlib.closing(self.connect()) as c:
            value['attention'] = blocking(c,row['repository'],row['number'])
            value['reconciliation'] = reconciliation(value,worker_state(c))
        if value['status'] == 'superseded': value['stage'] = '版本已过期'
        value['configuration'] = json.loads(cfg[0]) if cfg else None
        for key in ('snapshot', 'result', 'diagnostic', 'raw_diagnostics', 'session_counters'):
            value[key] = json.loads(value[key]) if value[key] else None
        if not public and value.get('snapshot'):
            from .snapshot import hydrate
            hydrate(value['snapshot'], self)
        from .progress_state import job_progress
        value['progress'] = job_progress(value)
        value['progress']['sync_error'] = sync['progress_error'] if sync else ''
        value['progress']['synced_at'] = sync['progress_sent_at'] if sync else None
        if (public or evidence) and value.get('plan_units') and value.get('result') is not None:
            from .review_state import aggregate
            value['result'] = aggregate(self, value, evidence=evidence)
        if public:
            if value.get('batch_protocol'):
                from .batch_store import get, timing
                value['batch'] = get(self, job)
                value['timing'] = timing(self, job)
            from .performance import summary
            if value.get('configuration'):
                value['configuration'].pop('api_key', None)
            value['performance']=summary(self,job)
            value.pop('owner', None)
            value.pop('event_token_hash', None)
            value.pop('native_result_sha256', None)
            if value['snapshot']:
                value['snapshot'] = {k: value['snapshot'][k] for k in ('id', 'base', 'head', 'merge_base', 'coverage', 'analysis_version') if k in value['snapshot']}
            from .session_events import project
            project(value)
        return value

    def jobs(self, page=1, repository=None, number=None, status=None, allowed_jobs=None, window=None, allowed_repositories=None):
        from .attention import STATES
        if status is not None and status not in STATES: raise ReviewError('不支持的任务状态')
        conditions, args = [], []
        for field, value in (('repository',repository),('number',number),('status',status)):
            if value is not None: conditions.append(field+'=?'); args.append(value)
        if window is not None:
            conditions.extend(['created>=?', 'created<?'])
            args.extend([window['lower'], window['upper']])
        if allowed_repositories is not None:
            conditions.append('repository IN (SELECT value FROM json_each(?))')
            args.append(json.dumps(allowed_repositories))
        where = ' WHERE '+' AND '.join(conditions) if conditions else ''
        if allowed_jobs is not None:
            conditions.append('id IN (SELECT value FROM json_each(?))')
            args.append(json.dumps(allowed_jobs))
            where = ' WHERE ' + ' AND '.join(conditions)
        columns = ('batch_protocol,id,repository,number,status,stage,created,updated,config_revision,repo_revision,'
                   'generation,lease,model_calls,started_at,finished_at,progress_stage,units_completed,'
                   "units_total,target_head,review_id,review_fragment,error,batch_index,batch_calls,plan_units,"
                   "session_id,session_status,current_action,deadline_at,last_heartbeat,log_synced_at,event_count,final_event_seq,"
                   "event_acked_seq,log_stream_closed,"
                   "json_extract(result,'$.coverage.complete') AS coverage_complete,"
                   "json_extract(result,'$.units_failed') AS units_failed,"
                   "json_extract(result,'$.units_processed') AS units_processed")
        with contextlib.closing(self.connect()) as c:
            c.execute('BEGIN')
            total = c.execute('SELECT count(*) FROM jobs' + where, args).fetchone()[0]
            values = [dict(r) for r in c.execute('SELECT ' + columns + ' FROM jobs' + where +
                      ' ORDER BY created DESC,id DESC LIMIT 20 OFFSET ?', args + [(page-1)*20])]
        from .progress_state import job_progress
        for value in values:
            value['result']={'coverage':{'complete':value.pop('coverage_complete') != 0},'units_failed':value.pop('units_failed') or 0,'units_processed':value.pop('units_processed') or value.get('units_completed',0)}
            value['progress'] = job_progress(value)
            value.pop('result',None)
            if value.get('batch_protocol'):
                from .batch_store import timing
                value['timing'] = timing(self, value['id'])
            from .session_events import project
            project(value)
        return {'items': values, 'total': total}

    def claim(self):
        now, owner = time.time(), secrets.token_hex(16)
        with self.tx() as c:
            c.execute("UPDATE review_batch_plans SET uncertain_time=1 WHERE job IN (SELECT id FROM jobs WHERE status IN ('running','publishing','recovering') AND lease<?)", (now,))
            # 中断后的无人监督区间单独记录，不能算成精确执行耗时。
            c.execute("INSERT INTO review_batch_intervals(job,state,started,ended,verified_until) SELECT i.job,'unknown',i.verified_until,?,? FROM review_batch_intervals i JOIN jobs j ON j.id=i.job WHERE i.ended IS NULL AND j.status IN ('running','publishing','recovering') AND j.lease<?", (now, now, now))
            c.execute("UPDATE review_batch_intervals SET ended=verified_until WHERE ended IS NULL AND job IN (SELECT id FROM jobs WHERE status IN ('running','publishing','recovering') AND lease<?)", (now,))
            # 发布中的请求可能已经生效，中断后只能进入核对流程。
            c.execute("UPDATE jobs SET status='publish_unknown',owner='',updated=?,error='执行中断，等待核对实际发布结果' WHERE status IN ('publishing','recovering') AND lease<?", (now, now))
            c.execute("UPDATE jobs SET status='failed',stage='中断次数达到上限',error='已中断三次，请排查环境后人工重审',updated=? WHERE status='running' AND lease<? AND attempts>=3", (now, now))
            c.execute("UPDATE jobs SET status='queued',owner='',available=?,updated=?,stage='中断后重新领取' WHERE status='running' AND lease<?", (now, now, now))
            if not self.config(connection=c)['enabled']:
                return None
            if c.execute("SELECT 1 FROM jobs WHERE status='cancelling' LIMIT 1").fetchone():
                return None
            if c.execute("SELECT 1 FROM jobs WHERE status IN ('running','publishing','recovering') AND lease>=? LIMIT 1", (now,)).fetchone():
                return None
            row = c.execute("""SELECT j.id FROM jobs j WHERE j.status='queued' AND j.available<=?
                               AND NOT EXISTS (SELECT 1 FROM jobs x WHERE x.repository=j.repository AND x.number=j.number
                                               AND x.status='publish_unknown') ORDER BY j.available,j.created LIMIT 1""", (now,)).fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET status='running',stage='读取固定版本',owner=?,lease=?,updated=?,attempts=attempts+1 WHERE id=?", (owner, now+90, now, row[0]))
        return self.job(row[0])

    def _active(self, c, job, owner):
        row = c.execute('''SELECT j.id,j.owner,j.status,j.lease,j.generation,j.config_revision,j.repo_revision,r.enabled,r.revision AS current_repo_revision,p.generation AS current_generation
                           FROM jobs j JOIN repositories r ON r.id=j.repository
                           JOIN pr_state p ON p.repository=j.repository AND p.number=j.number WHERE j.id=?''', (job,)).fetchone()
        cfg = self.config(connection=c)
        if (not row or row['owner'] != owner or row['status'] not in ('running', 'publishing', 'recovering')
                or row['lease'] < time.time() or row['generation'] != row['current_generation']
                or not row['enabled'] or not cfg['enabled'] or self.execution_revision_for(row['config_revision'], c) != execution_revision(cfg)
                or row['repo_revision'] != row['current_repo_revision']):
            raise Stale('任务版本、配置或执行租约已过期')
        return row

    def active(self, job, owner):
        with contextlib.closing(self.connect()) as c:
            return dict(self._active(c, job, owner))

    def update(self, job, owner, **fields):
        allowed = {'status', 'stage', 'snapshot', 'result', 'review_id', 'error',
                   'progress_stage', 'units_completed', 'units_total', 'target_head', 'review_fragment',
                   'diagnostic', 'raw_diagnostics', 'batch_index', 'batch_calls', 'batch_started', 'plan_units',
                   'session_id', 'session_status', 'current_action', 'deadline_at', 'last_heartbeat',
                   'log_synced_at', 'event_count', 'final_event_seq', 'event_acked_seq',
                   'log_stream_closed', 'native_result_sha256', 'native_result_released',
                   'session_counters', 'workspace_path'}
        if not fields or set(fields) - allowed:
            raise ValueError('不支持的任务字段')
        values = [encode(v) if k in ('snapshot', 'result', 'diagnostic', 'raw_diagnostics', 'session_counters') else v for k, v in fields.items()]
        with self.tx() as c:
            self._active(c, job, owner)
            c.execute('UPDATE jobs SET ' + ','.join(k+'=?' for k in fields) + ',updated=? WHERE id=? AND owner=?', values+[time.time(), job, owner])
            snapshot = fields.get('snapshot')
            if snapshot and snapshot.get('head') and snapshot.get('base'):
                # 人工入队尚不知道 SHA。固定输入一旦取得，就原子登记本代实际版本。
                # 即使随后模型失败或进程中断，定期扫描也不能把相同版本当成新修改重跑。
                row = c.execute('SELECT repository,number,generation,config_revision,repo_revision FROM jobs WHERE id=?', (job,)).fetchone()
                signature = encode([{'head':snapshot['head'], 'base':snapshot['base']}, self.execution_revision_for(row['config_revision'], c), row['repo_revision'], False])
                c.execute('UPDATE pr_state SET signature=? WHERE repository=? AND number=? AND generation=?',
                          (signature, row['repository'], row['number'], row['generation']))

    def finish(self, job, owner, status, error=''):
        with self.tx() as c:
            # 只改变本执行者仍持有的行；已被新事件取消的任务不得复活。
            c.execute("UPDATE jobs SET status=?,stage=?,error=?,updated=?,lease=0 WHERE id=? AND owner=? AND status IN ('running','publishing','recovering')",
                      (status, {'succeeded': '已完成', 'superseded': '版本已过期', 'failed': '执行失败',
                                'publish_unknown': '发布结果待核对'}.get(status, status), error[:1000], time.time(), job, owner))
            # 过期进程结束时只能撤销自己的授权，不能删除已重新领取者的授权。
            c.execute('DELETE FROM capabilities WHERE job=? AND owner=?', (job, owner))

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
            # 保留发布歧义证据；新台账任务永久保留完整 diff 与报告，旧源码任务仍按 30 天释放。
            for table in ('review_turns','review_reads','review_source_cache','review_model_inputs'):
                c.execute(f"DELETE FROM {table} WHERE job IN (SELECT id FROM jobs WHERE updated<? AND (status IN ('failed','superseded','cancelled') OR (status='succeeded' AND id IN (SELECT job FROM review_archive_jobs))))", (cutoff,))
            c.execute("DELETE FROM review_units WHERE job IN (SELECT id FROM jobs WHERE updated<? AND (status IN ('failed','superseded','cancelled') OR (status='succeeded' AND id IN (SELECT job FROM review_archive_jobs))))", (cutoff,))
            c.execute("UPDATE jobs SET snapshot=NULL,result=NULL WHERE COALESCE(json_extract(snapshot,'$.lifecycle_version'),0)!=1 AND updated<? AND (status IN ('failed','superseded','cancelled') OR (status='succeeded' AND id IN (SELECT job FROM review_archive_jobs)))", (cutoff,))
            c.execute("DELETE FROM review_feedback_events WHERE status='done' AND due<?", (cutoff,))
