"""附件固定清单、来源正文链接替换与逐文件校验。"""
import uuid
import hashlib
import re
from urllib.parse import urlsplit, quote, unquote
from svn_migration.common import require
from .files import SourceFiles
from .normalize import identifier, timestamp


def plan_assets(inventory, kind, normalized, key, source_base, target_base):
    assets, replacements, handled = [], {}, set()
    if kind == 'gitlab':
        return gitlab_assets(inventory, normalized, key, source_base, target_base)
    if kind != 'gitea':
        return assets
    issues = {obj['number']: obj for obj in normalized['objects'] if obj['kind'] in ('issue', 'pull')}
    for dataset in inventory['datasets']:
        category = dataset['category']
        if category not in ('议题附件', '发布附件'):
            continue
        path = dataset['path']
        if category == '议题附件':
            number = int(path.split('/issues/')[1].split('/')[0])
            parent = issues.get(number)
            require(parent is not None, '附件所属议题未纳入迁移')
            parent_kind, parent_id = parent['kind'], parent['source_id']
        else:
            parent_kind, parent_id = 'release', path.split('/releases/')[1].split('/')[0]
            require(any(o['kind'] == 'release' and o['source_id'] == parent_id for o in normalized['objects']), '附件所属发布尚未迁移')
        for row in dataset['rows']:
            source_id = 'attachment-' + identifier(row['id'])
            url = row['browser_download_url']
            require(isinstance(url, str) and urlsplit(url).scheme in ('http', 'https'), '附件下载地址无效')
            metadata = {'source_id': source_id, 'name': row['name'], 'parent_kind': parent_kind,
                        'parent_id': parent_id, 'size': row['size'], 'created': timestamp(row['created_at'])}
            if parent_kind == 'release':
                release = next(obj for obj in normalized['objects'] if obj['kind'] == 'release' and obj['source_id'] == parent_id)
                download = source_base + '/' + inventory['project']['path'] + '/releases/download/' + quote(release['tag'], safe='') + '/' + quote(row['name'], safe='')
            else:
                uuid.UUID(row['uuid'])
                download = source_base + '/attachments/' + row['uuid']
            assets.append({'url': download, 'metadata': metadata})
            destination = urlsplit(target_base).path.rstrip('/') + '/attachments/' + str(uuid.uuid5(uuid.NAMESPACE_OID, key + '/' + source_id))
            replacements[url] = destination
            replacements[urlsplit(url).path] = destination
        handled.add(path)
    for obj in normalized['objects']:
        content = obj.get('content', '')
        for old, new in sorted(replacements.items(), key=lambda pair: -len(pair[0])):
            content = content.replace(old, new)
        if 'content' in obj:
            obj['content'] = content
    normalized['pending'] = [item for item in normalized['pending'] if item.get('path') not in handled]
    return assets


def transfer_assets(provider, receiver, key, assets, directory, checkpoint, save):
    if any(asset.get('web') for asset in assets):
        from .gitlab_web import authenticate_web
        authenticate_web(provider)
    files = SourceFiles(provider, directory, tick=receiver.process.tick)
    for asset in assets:
        metadata = dict(asset['metadata'])
        downloads = checkpoint.setdefault('downloads', {})
        urls = checkpoint.setdefault('download_urls', {})
        url_key = hashlib.sha256(asset['url'].encode()).hexdigest()
        local = downloads.get(metadata['source_id'])
        if local is None:
            local = urls.get(url_key)
            if local is None:
                local = files.download(asset['url'], expected_size=metadata.get('size'),
                                       expected_sha256=asset.get('expected_sha256'), follow_same_origin=True)
                urls[url_key] = local
            require(metadata.get('size') is None or metadata['size'] == local['size'], '同一来源文件出现不同大小')
            require(not asset.get('expected_sha256') or asset['expected_sha256'] == local['sha256'], '同一来源文件出现不同摘要')
            downloads[metadata['source_id']] = local
            save(checkpoint)
        metadata['sha256'] = local['sha256']
        metadata['size'] = local['size']
        receiver.import_asset(key, metadata, local['file'], checkpoint, save)


def gitlab_assets(inventory, normalized, key, source_base, target_base):
    assets = []
    project_path = '/' + inventory['project']['path']
    for obj in normalized['objects']:
        if obj['kind'] not in ('issue', 'pull', 'comment', 'code_comment', 'release'):
            continue
        content = obj.get('content', '')
        links = set(re.findall(r'(?:https?://|/)[^\s<>"\)]+', content))
        for link in sorted(links):
            parsed = urlsplit(link)
            match = re.search(r'/uploads/[0-9a-fA-F]{32}/[^/]+$', parsed.path)
            if not match:
                continue
            require(not parsed.netloc or parsed.hostname == urlsplit(source_base).hostname,
                    '正文附件来自另一台来源，不能自动合并')
            require(parsed.path[:match.start()] in ('', project_path), '正文附件属于另一项目，需先明确迁移关系')
            upload = match.group()
            source_id = 'upload-' + hashlib.sha256((obj['kind'] + ':' + obj['source_id'] + ':' + upload).encode()).hexdigest()
            metadata = {'source_id': source_id, 'name': unquote(upload.rsplit('/', 1)[-1]),
                        'parent_kind': obj['kind'], 'parent_id': obj['source_id'], 'created': obj['created']}
            assets.append({'web': True, 'url': source_base + project_path + upload, 'metadata': metadata})
            destination = urlsplit(target_base).path.rstrip('/') + '/attachments/' + str(uuid.uuid5(uuid.NAMESPACE_OID, key + '/' + source_id))
            content = content.replace(link, destination)
        obj['content'] = content
    if assets:
        normalized['pending'] = [row for row in normalized['pending'] if row.get('category') != 'GitLab 正文附件']
    return assets
