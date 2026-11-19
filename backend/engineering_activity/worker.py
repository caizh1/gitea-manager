"""双进程共用持久化调度，逐仓提交检查点。"""
import fcntl
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from access.identity import created_value
from access.models import OrganizationBinding
from access.scopes import binding_for, repositories_for, server_epoch
from models import GiteaServer, db
from services.policy_gitea import PolicyGitea, PolicyError
from services.upgrade_maintenance import active

from .collector import RefPending, collect_repo, page_rows
from .models import Collection, Repository, Roster
from .rules import day, person

logger = logging.getLogger(__name__)
INTERVAL = timedelta(minutes=15)


def repository_key(row):
    return row['id'], row['owner']['id'], created_value(row.get('created_at'))


def catalog(client, binding):
    if binding:
        return repositories_for(binding)
    repos = client.pages('/repos/search', {'private': 'true'})
    return {repo['id']: repo for repo in repos}


def available_teams(client, binding):
    organizations = ({binding.organization_id: binding.organization_name} if binding else {
        row['id']: row.get('username') or row.get('name') for row in client.pages('/orgs')
    })
    teams = {}
    for org_id, name in sorted(organizations.items()):
        if not name:
            continue
        for team in page_rows(client, '/orgs/' + quote(name, safe='') + '/teams'):
            teams[team['id']] = {'id': team['id'], 'name': team['name'], 'organization': name,
                                 'organization_id': org_id}
    return teams


def scoped_states(server, scope):
    return Repository.query.filter_by(server_id=server.id, epoch=server_epoch(server), scope_id=scope)


def collect_rosters(client, collection, binding, now):
    selected = collection.settings['teams']
    if not selected:
        return
    teams = available_teams(client, binding)
    for team_id in selected:
        if team_id not in teams:
            continue
        members = list(page_rows(client, f'/teams/{team_id}/members'))
        key = (collection.server_id, collection.epoch, team_id, day(now))
        row = db.session.get(Roster, key)
        if row is None:
            row = Roster(server_id=key[0], epoch=key[1], team_id=key[2], day=key[3])
            db.session.add(row)
        # 保存当天观察到的成员并集，跨日保留快照。
        observed = dict(row.members or {})
        observed.update(dict(person(user) for user in members if person(user)[0] != 'unknown'))
        row.members = observed
        row.organization_id = teams[team_id]['organization_id']


def execute(key, now):
    task = db.session.get(Collection, key)
    if task is None or task.due > now:
        return
    version = task.requested
    task.status = 'collecting'
    task.started = now
    task.progress = {}
    db.session.commit()
    total = complete = errors = 0
    problem = ''
    try:
        server = db.session.get(GiteaServer, key[0])
        if server is None or server_epoch(server) != key[1]:
            task.status, task.due = 'disabled', datetime.max.replace(microsecond=0)
            db.session.commit()
            return
        settings = task.settings
        platform = db.session.get(Collection, (key[0], key[1], 0))
        callback = platform.settings.get('webhook_base', '') if platform else ''
        binding = None
        if key[2]:
            binding_id = OrganizationBinding.query.filter_by(server_id=key[0], instance_epoch=key[1],
                                                             organization_id=key[2], enabled=True).first()
            if binding_id is None:
                task.status, task.due = 'disabled', datetime.max.replace(microsecond=0)
                db.session.commit()
                return
            binding = binding_for(binding_id.id)
        client = PolicyGitea(server)
        repositories = catalog(client, binding)
        try:
            activity_visible = bool(client.request('GET', '/user').get('is_admin'))
        except PolicyError:
            activity_visible = False
        current_keys = {repository_key(repo) for repo in repositories.values()}
        for stored in scoped_states(server, key[2]).all():
            if (stored.repo_id, stored.organization_id, stored.birth) not in current_keys:
                # 曾观测到转移、删除或失权的仓库重新纳入后必须重新采集。
                from .models import Activity, Daily
                Daily.query.filter_by(repository=stored.id).delete()
                Activity.query.filter_by(repository=stored.id).delete()
                db.session.delete(stored)
        db.session.commit()
        total = len(repositories)
        for info in sorted(repositories.values(), key=lambda row: row.get('updated_at') or '', reverse=True):
            if active():
                return
            if server_epoch(db.session.get(GiteaServer, key[0], populate_existing=True)) != key[1]:
                raise PolicyError('Gitea 实例身份已变更')
            repo_id, org_id, birth = repository_key(info)
            state = scoped_states(server, key[2]).filter_by(repo_id=repo_id, organization_id=org_id, birth=birth).first()
            if state is None:
                state = Repository(server_id=key[0], epoch=key[1], scope_id=key[2],
                                   repo_id=repo_id, organization_id=org_id, birth=birth, name=info['full_name'])
                db.session.add(state)
                db.session.commit()
            try:
                # 逐仓重新核实归属，避免旧目录对正在迁移的仓库入库。
                live = client.request('GET', f'/repositories/{repo_id}')
                if repository_key(live) != (repo_id, org_id, birth):
                    raise PolicyError('仓库归属正在变更')
                days = collect_repo(client, live, state, settings, datetime.utcnow(), callback, activity_visible)
                state.name = live['full_name']
                from .report import materialize
                materialize(state, settings['paths'].get(str(repo_id), {}), days)
                if active():
                    db.session.rollback()
                    return
                db.session.commit()
                complete += 1
                if state.health.get('hook_error'):
                    errors += 1
                    problem = state.health['hook_error']
            except Exception as exc:
                db.session.rollback()
                errors += 1
                problem = str(exc) if isinstance(exc, PolicyError) else '仓库数据或 Git 对象读取失败'
                logger.warning('研发上库仓库 %s 采集失败（%s）', repo_id, type(exc).__name__)
                state = db.session.get(Repository, state.id)
                state.health = {**state.health, 'error': problem}
                if isinstance(exc, RefPending) and not state.health.get('awaiting_refs_since'):
                    state.health = {**state.health, 'awaiting_refs_since': datetime.utcnow().isoformat() + 'Z'}
                db.session.commit()
            task = db.session.get(Collection, key, populate_existing=True)
            task.progress = {'complete': complete, 'total': total, 'errors': errors, 'error': problem}
            db.session.commit()
        if not active():
            collect_rosters(client, task, binding, datetime.utcnow())
    except Exception as exc:
        db.session.rollback()
        errors += 1
        problem = str(exc) if isinstance(exc, PolicyError) else '组织目录或研发名单采集失败'
        logger.warning('研发上库实例 %s 采集失败（%s）', key[0], type(exc).__name__)
    if active():
        db.session.rollback()
        return
    finished = datetime.utcnow()
    identity = Collection.query.filter_by(server_id=key[0], epoch=key[1], organization_id=key[2])
    result = {'progress': {'complete': complete, 'total': total, 'errors': errors, 'error': problem},
              'finished': finished}
    if not identity.filter_by(requested=version).update(
        {**result, 'status': 'partial' if errors else 'ready', 'due': finished + INTERVAL}
    ):
        identity.update(result)
    db.session.commit()


def tick():
    if active():
        return
    lock = Path(db.engine.url.database).resolve().parent / '.engineering-activity.lock'
    with lock.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        now = datetime.utcnow()
        next_task = Collection.query.filter(Collection.due <= now).order_by(Collection.due).first()
        if next_task:
            execute((next_task.server_id, next_task.epoch, next_task.organization_id), now)


def start(app):
    def run():
        wake = threading.Event()
        while True:
            try:
                with app.app_context():
                    tick()
            except Exception:
                logger.exception('研发上库调度失败')
            wake.wait(15)

    threading.Thread(target=run, daemon=True, name='engineering-activity').start()
