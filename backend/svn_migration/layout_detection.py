"""有界只读检测 SVN 目录；候选来自实际目录，歧义不自动猜选。"""
from collections import deque
from datetime import datetime
import json
import time
from urllib.parse import quote
import xml.etree.ElementTree as ET

from models import db
from .common import MigrationError, Cancelled, encode, require, source_url, within_source, layout, file_lock, work_root
from .models import SvnSource, SvnLayoutDetection

# 别名只用于提出候选，目录仍必须真实存在；不改写大小写。
NAMES = {'trunk': {'trunk', 'main', 'mainline'},
         'branches': {'branches', 'devlines'}, 'tags': {'tags', 'releases'}}
MAX_LISTS, MAX_ENTRIES, MAX_DEPTH = 48, 2000, 2


def public(row):
    source = db.session.get(SvnSource, row.source_id, populate_existing=True)
    return {'id': row.id, 'source_id': row.source_id, 'url': row.url,
            'status': row.status if source and not source.archived and source.version == row.source_version else 'stale',
            'result': json.loads(row.result_json), 'error': row.error,
            'created_at': row.created_at.isoformat()}


def queue(source, urls):
    require(not source.archived, '来源已停用')
    require(isinstance(urls, list) and 0 < len(urls) <= 1000, '请提供 1 至 1000 个仓库 URL')
    urls = [source_url(url) for url in urls]
    require(len(set(urls)) == len(urls), '清单中存在重复的仓库 URL，请保留一条后重试')
    require(all(within_source(url, source.url) for url in urls), '仓库 URL 必须位于所选来源地址下')
    with file_lock(work_root() / 'enqueue.lock', blocking=True):
        require(SvnLayoutDetection.query.filter(SvnLayoutDetection.status.in_(('queued', 'running'))).count() + len(urls) <= 1000,
                '目录检测队列已满，请等待现有任务完成', 'busy', 409)
        rows = [SvnLayoutDetection(source_id=source.id, source_version=source.version, url=url) for url in urls]
        db.session.add_all(rows)
        db.session.commit()
    return rows


def confirmed(source, data):
    """客户端只能选择服务器已检测到的目录，不能伪造检测结果或跨来源复用。"""
    require(type(data.get('detection_id')) is int, '目录检测编号无效')
    row = db.session.get(SvnLayoutDetection, data['detection_id'])
    require(row and row.source_id == source.id and row.source_version == source.version and not source.archived
            and row.url == source_url(data.get('url')), '目录检测已失效，请重新检测', 'stale', 409)
    require(row.status in ('ready', 'ambiguous'), '目录检测尚未成功，请先处理检测问题')
    result = json.loads(row.result_json)
    require(not result['limited'], '扫描达到边界，请缩小到具体项目 URL 后重新检测')
    selected = layout(data.get('layout', {}))
    allowed = {entry['path'] for entry in result['directories']} | {'.', ''}
    require(all(selected[key] in allowed for key in ('trunk', 'branches', 'tags')), '所选路径不在检测目录中')
    require(selected['trunk'] and (selected['trunk'] != '.' or not (selected['branches'] or selected['tags'])),
            '根目录整体迁移不能同时配置独立分支或标签目录')
    # 只有经过完整列举的目录才可以作为分支/标签容器。
    for key in ('branches', 'tags'):
        if selected[key]:
            directory = next(entry for entry in result['directories'] if entry['path'] == selected[key])
            require(directory.get('listed') and not directory.get('files'), '分支或标签目录未完整读取，或包含直接文件')
    return selected


def detect(source, url):
    """固定仓库 HEAD，广度优先扫描项目层级，跳过已识别布局的内容子树。"""
    started = time.monotonic()
    info = source.info(url)
    revision = source.info(info['root'])['revision']
    queue = deque([('', 0)])
    directories, candidates, children = {}, [], {}
    count, calls, limited = 0, 0, False

    def listing(path_url):
        # 大目录输出先落盘；超过限额就阻断，不把整份 XML 载入内存。
        path = source.file('list', '--xml', '-r', revision, path_url + '@' + str(revision), timeout=30)
        try:
            require(path.stat().st_size <= 4 * 1024 * 1024, '目录列表过大，请将 URL 缩小到具体项目', 'capacity')
            rows = []
            for _, entry in ET.iterparse(path, events=('end',)):
                if entry.tag == 'entry':
                    rows.append({'name': entry.findtext('name'), 'kind': entry.get('kind')})
                    entry.clear()
                    if len(rows) > MAX_ENTRIES:
                        break
            return rows
        except ET.ParseError:
            raise MigrationError('SVN 返回了无效的目录数据', 'source') from None
        finally:
            path.unlink(missing_ok=True)

    while queue:
        parent, depth = queue.popleft()
        if calls >= MAX_LISTS or time.monotonic() - started > 120:
            limited = True
            break
        path_url = url + ('/' + quote(parent, safe='/') if parent else '')
        rows = listing(path_url)
        calls += 1
        count += len(rows)
        if count > MAX_ENTRIES:
            limited = True
            break
        children[parent] = rows
        if parent:
            directories[parent].update(listed=True, files=sum(row['kind'] == 'file' for row in rows))
        names = [row['name'] for row in rows if row['kind'] == 'dir']
        for name in names:
            path = parent + '/' + name if parent else name
            directories[path] = {'path': path, 'listed': False}
        roles = {key: [name for name in names if name.lower() in aliases] for key, aliases in NAMES.items()}
        join = lambda name: parent + '/' + name if parent and name else name
        for trunk in roles['trunk']:
            for branches in roles['branches'] or ['']:
                for tags in roles['tags'] or ['']:
                    candidates.append({'layout': {'trunk': join(trunk), 'branches': join(branches), 'tags': join(tags), 'default_branch': 'main'},
                                       'reason': '同一项目目录中发现实际存在的主干、分支和标签候选'})
        # 分支、标签容器需读取一层，确认其子项是目录，避免把普通源码目录当容器。
        containers = roles['branches'] + roles['tags']
        for name in containers:
            path = join(name)
            if calls >= MAX_LISTS or time.monotonic() - started > 120:
                limited = True
                break
            sub = listing(url + '/' + quote(path, safe='/'))
            calls += 1
            count += len(sub)
            if count > MAX_ENTRIES:
                limited = True
                break
            directories[path].update(listed=True, files=sum(e['kind'] == 'file' for e in sub))
        if limited:
            break
        # 有明确布局的项目不继续钻进源码；其余兄弟项目仍独立检查。
        reserved = {name for values in roles.values() for name in values}
        for name in names:
            if name in reserved:
                continue
            path = join(name)
            if depth < MAX_DEPTH:
                queue.append((path, depth + 1))
            else:
                directories[path]['boundary'] = True
    # 深层目录可能只是源码，但不能把未扫描到的结构宣称为完整自动识别。
    warnings = []
    if limited:
        warnings.append('扫描达到目录数、条目数或耗时边界，请将 URL 缩小到具体项目后重试')
    candidates = [candidate for candidate in candidates if all(
        not candidate['layout'][key] or directories[candidate['layout'][key]].get('listed')
        and not directories[candidate['layout'][key]].get('files') for key in ('branches', 'tags'))]
    top = children.get('', [])
    if not candidates and not any(row['name'].lower() in NAMES['branches'] | NAMES['tags'] for row in top):
        candidates.append({'layout': {'trunk': '.', 'branches': '', 'tags': '', 'default_branch': 'main'},
                           'reason': '未发现明确布局，候选为整个登记目录；所有子目录作为主干内容保留'})
        warnings.append('未发现明确的主干/分支/标签布局，请确认是否将整个登记目录作为一个 Git 仓库')
    if len(candidates) > 1:
        warnings.append('发现多个候选项目或布局，请选择本次要迁移的项目；多个项目应分别登记')
    if not candidates:
        warnings.append('未能唯一识别主干，请从已读取的实际目录选择，或缩小仓库 URL 后重试')
    known = {candidate['layout'][key].split('/')[0] for candidate in candidates for key in ('trunk', 'branches', 'tags') if candidate['layout'][key] not in ('', '.')}
    extra = [row['name'] for row in top if row['name'] not in known]
    if candidates and candidates[0]['layout']['trunk'] != '.' and extra:
        warnings.append('候选布局之外还有目录或文件：' + '、'.join(extra) + '；请确认它们不属于迁移范围')
    if candidates and all(candidate['layout']['trunk'] != '.' for candidate in candidates):
        roots = [candidate['layout'][key] for candidate in candidates for key in ('trunk', 'branches', 'tags') if candidate['layout'][key]]
        outside = []
        for parent, entries in children.items():
            for entry in entries:
                path = parent + '/' + entry['name'] if parent else entry['name']
                if not any(path == root or path.startswith(root + '/') or entry['kind'] == 'dir' and root.startswith(path + '/') for root in roots):
                    outside.append(path)
        if outside:
            warnings.append('实际目录中存在候选范围之外的内容（含未深入扫描目录）：' + '、'.join(outside[:20]) + '；请缩小 URL 或确认迁移范围')
    return {'revision': revision, 'uuid': info['uuid'], 'directories': list(directories.values()),
            'entries': top, 'candidates': candidates, 'limited': limited, 'warnings': warnings,
            'requests': calls, 'elapsed_seconds': round(time.monotonic() - started, 3)}


def step(tick):
    from .engine import Process, Source
    from .service import source_runtime
    import tempfile
    # 本入口只由持有唯一执行器锁的串行 Worker 调用；上次中止的只读任务可安全重做。
    abandoned = SvnLayoutDetection.query.filter_by(status='running').first()
    if abandoned:
        abandoned.status = 'queued'
        db.session.commit()
    row = SvnLayoutDetection.query.filter_by(status='queued').order_by(SvnLayoutDetection.id).first()
    if not row:
        return False
    ident = row.id
    row.status = 'running'
    db.session.commit()
    folder = work_root() / ('layout-' + str(ident))
    folder.mkdir(mode=0o700, exist_ok=True)
    try:
        value = source_runtime(row.source_id, row.source_version)
        with tempfile.TemporaryDirectory(dir=folder, prefix='credentials-') as secret_dir:
            started = time.monotonic()
            def detection_tick():
                tick()
                require(time.monotonic() - started <= 120, '目录检测超过两分钟，请检查网络或缩小项目 URL', 'timeout')
            process = Process(folder, detection_tick, secrets=(value['password'],))
            result = detect(Source(value, process, secret_dir), row.url)
        db.session.expire_all()
        row = db.session.get(SvnLayoutDetection, ident)
        source_runtime(row.source_id, row.source_version)
        row.result_json = encode(result)
        row.status = 'ready' if len(result['candidates']) == 1 and not result['warnings'] else 'ambiguous'
    except MigrationError as exc:
        db.session.rollback()
        if exc.code == 'maintenance' or isinstance(exc, Cancelled):
            # 维护中不写失败回执；维护解除或执行器重启后再做只读检测。
            return True
        row = db.session.get(SvnLayoutDetection, ident)
        row.status, row.error = ('stale' if exc.code == 'stale' else 'failed'), str(exc)
    except Exception as exc:
        db.session.rollback()
        row = db.session.get(SvnLayoutDetection, ident)
        row.status, row.error = 'failed', '目录检测内部错误：' + type(exc).__name__
    row.completed_at = datetime.utcnow()
    db.session.commit()
    return True
