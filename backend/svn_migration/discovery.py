"""读取实例目录索引并逐项验证 SVN 身份；纳管整个项目，不推测普通目录的业务归属。"""
from datetime import datetime
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urljoin, urlsplit, unquote
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth
from models import db
from .common import MigrationError, Cancelled, require, source_url, within_source, encode, digest, file_lock, work_root
from .models import SvnSource, SvnDiscovery, SvnRepository

MAX_PROJECTS = 256


class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.extend(v for k, v in attrs if k == 'href' and v)


def index_links(content, base):
    """只接受同来源目录的直接子项；不执行脚本，不跟随外部链接。"""
    parser = Links(); parser.feed(content)
    links = list(parser.links)
    try:
        root = ET.fromstring(content)
        links += [e.get('href') or e.get('name') + '/' for e in root.iter('dir') if e.get('href') or e.get('name')]
    except ET.ParseError:
        pass
    result = set()
    base = source_url(base)
    for link in links:
        try:
            url = source_url(urljoin(base + '/', link))
        except (MigrationError, ValueError):
            continue
        if not within_source(url, base) or url == base:
            continue
        suffix = urlsplit(url).path[len(urlsplit(base).path):].strip('/')
        if '/' not in suffix and suffix:
            result.add(url)
    require(len(result) <= MAX_PROJECTS, '实例列表超过 256 项，请使用更具体的入口或分批补充项目地址', 'capacity')
    return sorted(result)


def read_index(record, url, tick):
    """鉴权请求限时限量，重定向必须仍在同一已配置来源内。"""
    ca = None
    url = url.rstrip('/') + '/'
    with tempfile.TemporaryDirectory(prefix='index-', dir=work_root()) as folder, requests.Session() as session:
        session.trust_env = False
        if record['ca_pem']:
            ca = Path(folder) / 'ca.pem'; ca.write_text(record['ca_pem']); ca.chmod(0o600)
        auth = HTTPBasicAuth(record['username'], record['password'])
        try:
            for attempt in range(5):
                tick()
                with session.get(url, auth=auth, verify=str(ca) if ca else True,
                                 timeout=(8, 15), allow_redirects=False, stream=True) as response:
                    if response.status_code == 401 and 'digest' in response.headers.get('WWW-Authenticate', '').lower() and not isinstance(auth, HTTPDigestAuth):
                        auth = HTTPDigestAuth(record['username'], record['password']); continue
                    require(response.status_code != 401, '账号、密码或认证方式不正确，请修改连接信息后重试', 'authentication')
                    if response.is_redirect:
                        target = urljoin(url, response.headers.get('Location', ''))
                        require(within_source(source_url(target), record['url']), '列表重定向离开配置来源，已停止；请填写实际 SVN 入口', 'source')
                        url = target; continue
                    require(response.status_code == 200, '服务器未提供可读取的项目列表', 'listing')
                    data = bytearray()
                    for chunk in response.iter_content(65536):
                        tick(); data.extend(chunk)
                        require(len(data) <= 2 * 1024 * 1024, '实例列表响应超过读取限额', 'capacity')
                    return index_links(data.decode('utf-8', errors='replace'), url)
            raise MigrationError('实例列表重定向次数过多', 'listing')
        except requests.RequestException as exc:
            # 不输出请求对象，避免认证头、代理与密码进入日志。
            raise MigrationError('无法读取实例列表，请检查地址、网络或证书（' + type(exc).__name__ + '）', 'connection') from None


def public(row):
    source = db.session.get(SvnSource, row.source_id, populate_existing=True)
    result = json.loads(row.result_json)
    managed = {r.url: r for r in SvnRepository.query.filter_by(source_id=row.source_id, archived=False)}
    for item in result.get('projects', []):
        item['managed_id'] = managed[item['url']].id if item['url'] in managed else None
    return {'id': row.id, 'source_id': row.source_id, 'source_version': row.source_version,
            'status': row.status if source and not source.archived and source.version == row.source_version else 'stale',
            'result': result, 'error': row.error}


def queue(source, url=None):
    require(not source.archived, '来源已停用')
    url = source_url(url or source.url)
    require(within_source(url, source.url), '项目地址必须位于当前来源内')
    with file_lock(work_root() / 'enqueue.lock', blocking=True):
        existing = SvnDiscovery.query.filter_by(source_id=source.id, source_version=source.version, url=url).filter(SvnDiscovery.status.in_(('queued', 'running'))).first()
        if existing:
            return existing
        require(SvnDiscovery.query.filter(SvnDiscovery.status.in_(('queued', 'running'))).count() < 1000, '发现队列已满，请稍后重试', 'busy', 409)
        row = SvnDiscovery(source_id=source.id, source_version=source.version, url=url)
        db.session.add(row); db.session.commit()
        return row


def project(source, url, info):
    entries = source.xml('list', '--xml', '-r', info['revision'], url + '@' + str(info['revision']), timeout=30).findall('list/entry')
    require(len(entries) <= 2000, '项目入口目录过大，请稍后使用具体项目地址', 'capacity')
    name = unquote(urlsplit(url).path.rstrip('/').split('/')[-1]) or 'project'
    safe = re.sub('[^A-Za-z0-9_.-]+', '-', name).strip('-_.')[:105] or 'project'
    if not safe[0].isalnum(): safe = 'project-' + safe
    if safe.lower().endswith('.git'): safe += '-svn'
    if safe != name: safe += '-' + digest(url)[:8]
    return {'key': digest([url, info['uuid']])[:24], 'name': safe, 'label': name, 'url': url,
            'uuid': info['uuid'], 'revision': info['revision'],
            'entries': [{'name': e.findtext('name'), 'kind': e.get('kind')} for e in entries]}


def projects_at(source, url, info):
    """独立仓库可含同层标准项目；只有所有顶层内容均归属于明确项目时才拆分展示。"""
    from .layout_detection import NAMES
    from urllib.parse import quote
    item = project(source, url, info)
    entries = item['entries']
    if (not entries or len(entries) > 32 or any(e['kind'] != 'dir' for e in entries)
            or any(e['name'].lower() in set().union(*NAMES.values()) for e in entries)):
        return [item]
    children = []
    for entry in entries:
        child_url = url + '/' + quote(entry['name'], safe='')
        try:
            child = project(source, child_url, source.info(child_url))
        except MigrationError as exc:
            if exc.code in ('maintenance', 'cancelled', 'timeout', 'capacity'): raise
            return [item]
        if not any(e['kind'] == 'dir' and e['name'].lower() in NAMES['trunk'] for e in child['entries']):
            return [item]
        children.append(child)
    return children


def discover(record, url, process, secret_dir, update):
    from .engine import Source
    source = Source(record, process, secret_dir)
    # 给定入口已经是 SVN 目录时，该目录整体就是一个项目；不能把 test_spec 猜成独立项目。
    try:
        info = source.info(url)
    except MigrationError as exc:
        if exc.code != 'command': raise
    else:
        items = projects_at(source, url, info); update(items, 1, 1)
        return {'projects': items, 'connected': True, 'message': '已读取项目，可直接勾选纳管'}
    if urlsplit(url).scheme == 'svn':
        return {'projects': [], 'connected': False, 'message': 'svn 协议入口不提供实例项目列表，请补充一个已知项目地址'}
    # HTTP 集合入口既支持根路径，也支持默认 /svn 前缀；不猜任何项目名称。
    endpoints = [url]
    if not urlsplit(url).path.strip('/'):
        endpoints.append(url + '/svn')
    # 首页中的 /svn/、/repos/ 等导航不是仓库。候选验证失败后仍读取有界的
    # 子级索引；默认入口也必须继续尝试，不能被首页的一个普通链接截断。
    pending = [(endpoint, 0) for endpoint in endpoints]
    visited, checked, reachable, errors = set(), set(), False, []
    items, rejected, entry_urls = [], 0, []
    while pending and len(visited) < 32:
        endpoint, depth = pending.pop(0)
        if endpoint in visited:
            continue
        visited.add(endpoint)
        try:
            links = read_index(record, endpoint, process.tick)
            reachable = True
        except MigrationError as exc:
            if exc.code in ('maintenance', 'cancelled', 'timeout'): raise
            errors.append(exc)
            continue
        found = False
        for candidate in links:
            if candidate in checked:
                continue
            checked.add(candidate)
            process.tick()
            try:
                info = source.info(candidate)
                existing = {item['key'] for item in items}
                additions = projects_at(source, candidate, info)
                items.extend(item for item in additions if item['key'] not in existing)
                require(len(items) <= MAX_PROJECTS, '发现项目超过 256 个，请分批使用具体入口', 'capacity')
                found = True
            except MigrationError as exc:
                if exc.code in ('maintenance', 'cancelled', 'timeout', 'capacity'): raise
                rejected += 1
                if depth < 2 and len(pending) + len(visited) < 32:
                    pending.append((candidate, depth + 1))
            update(items, len(checked), 0)
        if found:
            entry_urls.append(endpoint)
    if not reachable and errors and all(e.code == 'listing' for e in errors):
        return {'projects': [], 'connected': False, 'message': '服务可达，但当前入口未提供项目列表；请重试或补充已知项目地址'}
    if not items:
        authentication_error = next((error for error in errors if error.code == 'authentication'), None)
        if authentication_error:
            raise authentication_error
    require(reachable, str(errors[0]) if errors else '无法连接 SVN 实例', errors[0].code if errors else 'connection')
    names = {}
    for item in items:
        if item['name'].lower() in names:
            item['name'] += '-' + item['key'][:8]
        names[item['name'].lower()] = True
    return {'projects': items, 'connected': True, 'rejected': rejected, 'entry_urls': entry_urls,
            'message': '已发现 ' + str(len(items)) + ' 个可访问项目' if items else
                ('列表中的入口未通过 SVN 验证，请检查实际项目列表地址或读取权限' if checked else
                 '已读取入口页面，但未取得项目链接；可补充实际项目列表地址')}


def enroll(source, data):
    from .service import edit_repository
    require(type(data.get('discovery_id')) is int, '项目发现编号无效')
    row = db.session.get(SvnDiscovery, data.get('discovery_id'))
    require(row and row.source_id == source.id and row.source_version == source.version and not source.archived,
            '项目发现结果已失效，请重新发现', 'stale', 409)
    require(row.status == 'ready', '项目发现尚未完成')
    keys = data.get('keys')
    require(isinstance(keys, list) and 0 < len(keys) <= MAX_PROJECTS and all(isinstance(k, str) for k in keys) and len(set(keys)) == len(keys), '请选择有效且不重复的项目')
    items = {p['key']: p for p in json.loads(row.result_json)['projects']}
    require(all(k in items for k in keys), '所选项目不属于本次发现结果')
    result = []
    with file_lock(work_root() / 'enqueue.lock', blocking=True):
        # 条件写锁在纳管事务期间固定来源版本，防止改凭据后提交旧发现结果。
        require(SvnSource.query.filter_by(id=source.id, version=row.source_version, archived=False).update(
            {'tested_at': source.tested_at}, synchronize_session=False) == 1, '来源已变化，请重新发现', 'stale', 409)
        for key in keys:
            item = items[key]
            current = SvnRepository.query.filter_by(source_id=source.id, url=item['url']).first()
            if current:
                require(not current.archived, '项目已停用，请从已登记仓库恢复')
                result.append(current); continue
            result.append(edit_repository(source.id, {'name': item['name'], 'url': item['url'],
                'layout': {'trunk': '.', 'branches': '', 'tags': '', 'default_branch': 'main', 'project': True}}, commit=False))
        db.session.commit()
    return result


def step(tick):
    from .service import source_runtime
    from .engine import Process
    row = SvnDiscovery.query.filter(SvnDiscovery.status.in_(('queued', 'running'))).order_by(SvnDiscovery.id).first()
    if not row: return False
    ident, version = row.id, row.source_version
    row.status = 'running'; row.result_json = encode({'projects': [], 'processed': 0}); db.session.commit()
    started = time.monotonic()
    def check():
        tick(); require(time.monotonic() - started < 300, '项目发现超过五分钟，请缩小入口或分批补充项目地址', 'timeout')
    def update(items, count, total):
        from .service import source_runtime
        check(); source_runtime(row.source_id, version)
        row.result_json = encode({'projects': items, 'processed': count, 'total': total})
        db.session.commit()
    try:
        record = source_runtime(row.source_id, version)
        folder = work_root() / ('discovery-' + str(ident)); folder.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder, prefix='credentials-') as secret_dir:
            result = discover(record, row.url, Process(folder, check, secrets=(record['password'],)), secret_dir, update)
        check(); db.session.expire_all(); row = db.session.get(SvnDiscovery, ident)
        source_runtime(row.source_id, version)
        row.result_json = encode(result); row.status = 'ready' if result['projects'] else 'unavailable'
        source = db.session.get(SvnSource, row.source_id)
        source.test_json = encode({'status': 'success' if result['connected'] else 'unavailable', 'message': result['message']})
        source.tested_at = datetime.utcnow()
    except MigrationError as exc:
        db.session.rollback()
        if exc.code == 'maintenance' or isinstance(exc, Cancelled): return True
        row = db.session.get(SvnDiscovery, ident); row.status = 'stale' if exc.code == 'stale' else 'failed'; row.error = str(exc)
        source = db.session.get(SvnSource, row.source_id)
        if source and source.version == version:
            source.test_json = encode({'status': 'failed', 'message': str(exc)})
    except Exception as exc:
        db.session.rollback(); row = db.session.get(SvnDiscovery, ident)
        row.status, row.error = 'failed', '项目发现内部错误：' + type(exc).__name__
    row.completed_at = datetime.utcnow(); db.session.commit()
    return True
