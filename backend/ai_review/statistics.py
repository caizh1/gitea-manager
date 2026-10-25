"""只读统计：先限定可见任务，再聚合台账和人工反馈，不推测文本结论。"""
import contextlib
import json
import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime, time as day_time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import ReviewError


def period(args):
    """包含首尾自然日；按各日的当地午夜转换，兼容夏令时。"""
    zone = args.get('timezone', 'UTC')
    try:
        if not isinstance(zone, str) or len(zone) > 100:
            raise ValueError()
        tz = ZoneInfo(zone)
        today = datetime.now(tz).date()
        def parse(key, default):
            value = args.get(key)
            if value is None:
                return default
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError()
            return date.fromisoformat(value)
        end = parse('end', today)
        start = parse('start', end - timedelta(days=29))
        if not 0 <= (end - start).days < 366:
            raise ValueError()
        return dict(start=start.isoformat(), end=end.isoformat(), timezone=zone,
                    lower=datetime.combine(start, day_time(), tz).timestamp(),
                    upper=datetime.combine(end + timedelta(days=1), day_time(), tz).timestamp())
    except (ValueError, TypeError, OverflowError, ZoneInfoNotFoundError):
        raise ReviewError('日期或时区无效；请选择起止顺序正确、最多 366 天的日期范围') from None


def job_filter(window, repository=None, allowed_jobs=None, allowed_repositories=None):
    clauses = ['j.created>=?', 'j.created<?']
    values = [window['lower'], window['upper']]
    if repository is not None:
        clauses.append('j.repository=?'); values.append(repository)
    for column, allowed in [('j.id', allowed_jobs), ('j.repository', allowed_repositories)]:
        if allowed is not None:
            clauses.append(column + ' IN (SELECT value FROM json_each(?))')
            values.append(json.dumps(allowed))
    return ' AND '.join(clauses), values


def overview(store, window, repository=None, allowed_jobs=None, allowed_repositories=None):
    where, args = job_filter(window, repository, allowed_jobs, allowed_repositories)
    cohort = 'WITH selected AS (SELECT j.id,j.repository,j.number,j.created,j.status FROM jobs j WHERE ' + where + ') '
    tz = ZoneInfo(window['timezone'])
    daily = {}
    start, end = date.fromisoformat(window['start']), date.fromisoformat(window['end'])
    for offset in range((end - start).days + 1):
        day = (start + timedelta(days=offset)).isoformat()
        daily[day] = dict(date=day, succeeded=0, failed=0, other=0, p0=0, p1=0)
    def day_of(timestamp):
        return datetime.fromtimestamp(timestamp, tz).date().isoformat()

    statuses = Counter()
    tracked = {}
    source_identities = defaultdict(set)
    legacy = {}
    missing = 0
    unstructured = 0
    unknown_severity = 0
    # 同一个读事务保证卡片、曲线和反馈来自同一快照；不读取源码快照或报告全文。
    with contextlib.closing(store.connect()) as c:
        c.execute('BEGIN')
        for row in c.execute(cohort + 'SELECT * FROM selected', args):
            statuses[row['status']] += 1
            daily[day_of(row['created'])][row['status'] if row['status'] in ('succeeded', 'failed') else 'other'] += 1
        for row in c.execute(cohort + '''SELECT i.id,i.origin_job,i.repository,i.number,j.created,
                json_extract(i.finding,'$.severity') AS severity,
                json_extract(i.finding,'$.candidate_id') AS candidate,
                json_extract(i.finding,'$.fingerprint') AS fingerprint
              FROM review_issues i JOIN selected j ON i.origin_job=j.id
              AND i.repository=j.repository AND i.number=j.number''', args):
            severity = str(row['severity'] or '').upper()
            tracked[row['id']] = dict(severity=severity, created=row['created'])
            if severity in ('P0', 'P1'):
                daily[day_of(row['created'])][severity.lower()] += 1
            elif severity not in ('P2', 'P3'):
                unknown_severity += 1
            for field in ('candidate', 'fingerprint'):
                if row[field]:
                    source_identities[(row['origin_job'], field, row[field])].add(row['id'])

        # 只接受同一原始任务中的明确身份关联，禁止跨任务模糊匹配标题或行号。
        def canonical(job, finding):
            explicit = finding.get('issue_id')
            if explicit in tracked:
                return explicit
            matches = set()
            for field, key in [('candidate', 'candidate_id'), ('fingerprint', 'fingerprint')]:
                matches.update(source_identities.get((job, field, finding.get(key)), ()))
            return next(iter(matches)) if len(matches) == 1 else None

        archives = {}
        archived_identities = set()
        for row in c.execute(cohort + '''SELECT a.id,a.repository,a.number,j.id AS job,
                json_extract(a.value,'$.original') AS finding FROM review_finding_archive a
              JOIN selected j ON json_extract(a.value,'$.job')=j.id
                AND a.repository=j.repository AND a.number=j.number''', args):
            finding = json.loads(row['finding'] or '{}')
            identity = canonical(row['job'], finding) or 'archive:' + row['id']
            archives[row['id']] = identity
            for key in ('candidate_id', 'fingerprint'):
                if finding.get(key):
                    archived_identities.add((row['job'], key, finding[key]))
            if identity not in tracked:
                legacy[identity] = str(finding.get('severity') or '').upper()

        for row in c.execute(cohort + '''SELECT j.id,j.status,j.result IS NULL AS missing,
                json_extract(j.result,'$.findings') AS findings,
                json_extract(j.result,'$.lifecycle.version') AS lifecycle,
                json_extract(j.result,'$.inline_parser_version') AS parser,
                json_type(j.result,'$.report_markdown') AS report
              FROM jobs j JOIN selected s ON s.id=j.id''', args):
            if row['missing'] and row['status'] in ('succeeded', 'failed', 'cancelled', 'superseded'):
                missing += 1
            findings = json.loads(row['findings'] or '[]')
            if row['report'] and not findings:
                unstructured += 1
            if row['lifecycle']:
                continue
            for index, finding in enumerate(findings):
                if not isinstance(finding, dict) or canonical(row['id'], finding):
                    continue
                if any((row['id'], key, finding.get(key)) in archived_identities for key in ('candidate_id', 'fingerprint')):
                    continue
                key = 'result:' + row['id'] + ':' + str(finding.get('candidate_id') or finding.get('fingerprint') or index)
                legacy[key] = str(finding.get('severity') or '').upper()

        feedback = Counter(accepted=0, false_positive=0, other=0, conflict=0,
                           withdrawn=0, pending=0, unreviewed=0, unavailable=0)
        groups = defaultdict(list)
        # 根据归档原任务的可见性过滤；同仓库的其他归属代次也不能进入反馈统计。
        for row in c.execute(cohort + '''SELECT e.status,e.value,a.id FROM review_examples e
              JOIN review_finding_archive a ON e.target=a.id AND e.repository=a.repository AND e.number=a.number
              JOIN selected j ON json_extract(a.value,'$.job')=j.id
                AND a.repository=j.repository AND a.number=j.number
              WHERE json_extract(e.value,'$.sample_type')='ai_feedback' ''', args):
            if row['id'] in archives:
                groups[archives[row['id']]].append((row['status'], json.loads(row['value'])))
        supported = set(archives.values())
        for identity in supported:
            rows = groups[identity]
            active = [value for status, value in rows if status == 'active']
            labels = {(v.get('label'), v.get('correction', '')) for v in active}
            if len(labels) > 1:
                bucket = 'conflict'
            elif active:
                label = active[0].get('label')
                bucket = 'accepted' if label == 'valid' else 'false_positive' if label == 'false_positive' else 'other'
            elif any(status in ('pending', 'candidate', 'disabled') for status, _ in rows):
                bucket = 'pending'
            elif rows:
                bucket = 'withdrawn'
            else:
                bucket = 'unreviewed'
            feedback[bucket] += 1
        feedback['unavailable'] = len((set(tracked) | set(legacy)) - supported)

    severities = Counter(v['severity'] for v in tracked.values())
    return dict(period={k: window[k] for k in ('start', 'end', 'timezone')},
                summary=dict(total=sum(statuses.values()), succeeded=statuses['succeeded'], failed=statuses['failed'],
                             p0=severities['P0'], p1=severities['P1'],
                             accepted=feedback['accepted'] if supported else None),
                statuses=dict(statuses), daily=list(daily.values()), feedback=dict(feedback),
                coverage=dict(tracked=len(tracked), legacy=dict(Counter(legacy.values())),
                              legacy_total=len(legacy), feedback_supported=len(supported),
                              missing_results=missing, reports_without_findings=unstructured,
                              unknown_severity=unknown_severity), updated_at=time.time())
