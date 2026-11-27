"""整仓扫描任务、计划与持久化事件。"""
import contextlib
import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import ReviewError, encode
from .store import Store

FORMATS = ('html', 'md', 'json', 'txt', 'sarif', 'native')
ACTIVE = ('previewing', 'queued', 'running', 'retry', 'exporting', 'stopping')
SCHEMA = """
CREATE TABLE IF NOT EXISTS ocr_batches (
 id TEXT PRIMARY KEY, created REAL NOT NULL, actor TEXT NOT NULL, specification TEXT NOT NULL, plan TEXT);
CREATE TABLE IF NOT EXISTS ocr_targets (
 id TEXT PRIMARY KEY, batch TEXT NOT NULL REFERENCES ocr_batches(id), repository INTEGER NOT NULL,
 branch TEXT NOT NULL, sha TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, created REAL NOT NULL,
 updated REAL NOT NULL, ready REAL NOT NULL DEFAULT 0, attempt TEXT, broker TEXT,
 preview TEXT NOT NULL DEFAULT '{}', progress TEXT NOT NULL DEFAULT '{}', config TEXT NOT NULL,
 current TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', http INTEGER NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0,
 tokens INTEGER NOT NULL DEFAULT 0, reused TEXT, stop_actor TEXT, stopped_at REAL);
CREATE INDEX IF NOT EXISTS ocr_queue ON ocr_targets(state, ready);
CREATE INDEX IF NOT EXISTS ocr_branch ON ocr_targets(repository, branch, state);
CREATE TABLE IF NOT EXISTS ocr_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL REFERENCES ocr_targets(id),
 at REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ocr_timeline ON ocr_events(target, id);
CREATE TABLE IF NOT EXISTS ocr_event_receipts (
 target TEXT NOT NULL REFERENCES ocr_targets(id), native_id TEXT NOT NULL, PRIMARY KEY(target,native_id));
CREATE TABLE IF NOT EXISTS ocr_plans (
 id TEXT PRIMARY KEY, actor TEXT NOT NULL, specification TEXT NOT NULL, recurrence TEXT NOT NULL,
 next_at REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ocr_profiles (
 id INTEGER PRIMARY KEY CHECK(id=1), settings TEXT NOT NULL);
"""


def recurrence(value):
    try:
        frequency = value['frequency']
        zone = value['timezone']
        hour, minute = map(int, value['time'].split(':'))
        day = int(value.get('weekday', 0))
        if frequency not in ('daily', 'weekly') or not 0 <= hour < 24 or not 0 <= minute < 60 or not 0 <= day < 7:
            raise ValueError()
        ZoneInfo(zone)
        return {'frequency': frequency, 'timezone': zone, 'time': f'{hour:02d}:{minute:02d}', 'weekday': day,
                'force': value.get('force') is True}
    except (KeyError, TypeError, ValueError, AttributeError, ZoneInfoNotFoundError):
        raise ReviewError('定时规则无效') from None


def occurrences(rule, after, count=1):
    zone = ZoneInfo(rule['timezone'])
    current = datetime.fromtimestamp(after, timezone.utc).astimezone(zone)
    hour, minute = map(int, rule['time'].split(':'))
    result = []
    for offset in range(8 * count + 1):
        day = current.date() + timedelta(days=offset)
        if rule['frequency'] == 'weekly' and day.weekday() != rule['weekday']:
            continue
        local = datetime.combine(day, datetime.min.time(), tzinfo=zone).replace(hour=hour, minute=minute)
        value = local.timestamp()
        if value > after:
            result.append(value)
            if len(result) == count:
                break
    return result


def event(c, ident, kind, **detail):
    c.execute('INSERT INTO ocr_events(target,at,kind,detail) VALUES (?,?,?,?)',
              (ident, time.time(), kind, encode(detail)))


def update(c, ident, **fields):
    fields['updated'] = time.time()
    for name in ('preview', 'progress', 'config'):
        if name in fields:
            fields[name] = encode(fields[name])
    clause = ','.join(key+'=?' for key in fields)
    c.execute(f'UPDATE ocr_targets SET {clause} WHERE id=?', (*fields.values(), ident))


class ScanStore(Store):
    def __init__(self, path=None):
        super().__init__(path)
        with contextlib.closing(self.connect()) as c:
            c.executescript(SCHEMA)

    def profile(self, c=None):
        if c is None:
            with contextlib.closing(self.connect()) as conn:
                return self.profile(conn)
        row = c.execute('SELECT settings FROM ocr_profiles WHERE id=1').fetchone()
        return json.loads(row[0]) if row else {}

    def model(self, mode, c=None):
        profile = self.profile(c) if mode == 'custom' else self.config(connection=c)
        return {key: profile.get(key, '') for key in ('api_base_url', 'model', 'api_key', 'thinking_protocol', 'thinking_enabled')}

    def target(self, ident, c=None):
        if c is None:
            with contextlib.closing(self.connect()) as conn:
                return self.target(ident, conn)
        row = c.execute('''SELECT t.*, b.specification, b.actor, b.plan FROM ocr_targets t
                           JOIN ocr_batches b ON b.id=t.batch WHERE t.id=?''', (ident,)).fetchone()
        if not row:
            raise ReviewError('扫描目标不存在')
        value = dict(row)
        for name in ('specification', 'actor', 'preview', 'progress', 'config'):
            value[name] = json.loads(value[name])
        # 兼容旧任务和旧计划的持久数据，读取后按当前规则执行。
        value['specification'].pop('budget', None)
        value['config'].pop('resume_budget', None)
        return value

    def create(self, c, spec, actor, plan=None):
        spec = {key: value for key, value in spec.items() if key != 'budget'}
        model = self.model(spec['mode'], c)
        if not all(model.get(key) for key in ('api_base_url', 'api_key', 'model')):
            if not plan:
                raise ReviewError('请先配置扫描模型')
        ident = uuid4().hex
        now = time.time()
        c.execute('INSERT INTO ocr_batches VALUES (?,?,?,?,?)', (ident, now, encode(actor), encode(spec), plan))
        for target in spec['targets']:
            task = uuid4().hex
            c.execute('''INSERT INTO ocr_targets(id,batch,repository,branch,state,created,updated,config)
                         VALUES (?,?,?,?,?,?,?,?)''',
                      (task, ident, target['repository'], target['branch'], 'queued' if plan else 'previewing',
                       now, now, encode(model)))
            event(c, task, '定时入队' if plan else '预览入队')
        return ident

    def schedule(self, now):
        with self.tx() as c:
            rows = c.execute('SELECT * FROM ocr_plans WHERE enabled=1 AND next_at<=? ORDER BY next_at LIMIT 20', (now,)).fetchall()
            for row in rows:
                rule = json.loads(row['recurrence'])
                spec = dict(json.loads(row['specification']), force=rule['force'])
                self.create(c, spec, json.loads(row['actor']), row['id'])
                c.execute('UPDATE ocr_plans SET next_at=? WHERE id=?', (occurrences(rule, now)[0], row['id']))

    def attempt(self, ident):
        capability = secrets.token_hex(32)
        attempt = uuid4().hex
        with self.tx() as c:
            task = self.target(ident, c)
            if task['state'] not in ('queued', 'retry', 'running'):
                return None
            update(c, ident, state='running', attempt=attempt,
                   broker=hashlib.sha256(capability.encode()).hexdigest(), http=0, retry_at=0)
            event(c, ident, '开始 OCR 执行' if not task['attempt'] else '重新连接模型')
        return attempt, capability

    def report_dir(self, ident):
        return self.path.parent / 'ocr-reports' / ident
