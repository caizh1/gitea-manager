"""原生制品迁移清单：先固定并核验文件，再由封存接收会话发布。"""
import re
from urllib.parse import quote
from .normalize import identifier, timestamp
from svn_migration.common import require


def package_assets(inventory, kind, normalized, base):
    assets, handled = [], set()
    supported = {'generic', 'npm', 'maven', 'pypi', 'nuget', 'helm'}
    project_id = inventory['project']['id']
    for dataset in inventory['datasets']:
        if dataset['category'] != ('制品包' if kind == 'gitlab' else '所有者制品包'):
            continue
        all_supported = True
        for package in dataset['rows']:
            package_type = package.get('package_type' if kind == 'gitlab' else 'type')
            if package_type not in supported:
                all_supported = False
                continue
            require(kind!='gitlab' or package.get('status','default') in ('default','deprecated'), '来源制品仍在处理或不可安装，请等待完成后重新盘点')
            name, version = package['name'], package['version']
            require(isinstance(name, str) and name and len(name) <= 255 and not any(c in name for c in '\x00\r\n') and name != '..',
                    '来源通用制品名称不能无损映射到目标')
            require(isinstance(version, str) and version and version == version.strip(), '制品版本无效')
            ident = identifier(package['id'])
            owner = inventory['project'].get('path', '').split('/')[0]
            path = ('/projects/' + str(project_id) + '/packages/' + ident + '/package_files' if kind == 'gitlab' else
                    '/packages/' + quote(owner, safe='') + '/' + package_type + '/' + '/'.join(quote(v, safe='') for v in (name, version)) + '/files')
            files = next((row for row in inventory['datasets'] if row['path'] == path), None)
            require(files is not None and files['rows'], '制品文件清单缺失或为空')
            tags, metadata = [], {}
            if package_type == 'npm':
                index = next((d for d in inventory['datasets'] if d['category'] == '制品索引' and
                              d['rows'] and d['rows'][0].get('name') == name), None)
                require(index is not None, 'npm 制品缺少固定索引和分发标签')
                registry = index['rows'][0]
                value = registry.get('versions', {}).get(version)
                require(isinstance(value, dict) and value.get('name') == name and value.get('version') == version,
                        'npm 注册表版本元数据缺失或身份不符')
                metadata = dict(value)
                if registry.get('dist-tags', {}).get('latest') == version and 'readme' not in metadata and registry.get('readme'):
                    metadata['readme'] = registry['readme']
                tags = sorted(tag for tag, target in registry.get('dist-tags', {}).items() if target == version)
                handled.add(index['path'])
            normalized['objects'].append({'kind': 'package', 'source_id': ident,
                'title': name, 'created': timestamp(package['created_at']),
                'history': {'name': name.rsplit('/',1)[0].replace('/','.')+':'+name.rsplit('/',1)[1] if kind == 'gitlab' and package_type == 'maven' else re.sub(r'[-_.]+','-',name).lower() if package_type=='pypi' else name,
                            'version': version, 'type': package_type, 'source': package, 'tags': tags, 'metadata': metadata}})
            for file in files['rows']:
                filename = file['file_name' if kind == 'gitlab' else 'name']
                require(isinstance(filename, str) and filename and '/' not in filename and '\\' not in filename,
                        '来源制品文件名不兼容')
                sha = file.get('file_sha256' if kind == 'gitlab' else 'sha256')
                require(isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{64}', sha), '制品缺少源端 SHA-256')
                prefix = (base + '/api/v4/projects/' + str(project_id) + '/packages/generic/' if kind == 'gitlab' else
                          base + '/api/packages/' + quote(owner, safe='') + '/generic/')
                url = (prefix + '/'.join(quote(v, safe='') for v in (name, version, filename)) if package_type == 'generic' else
                       base + '/' + inventory['project']['path'] + '/-/package_files/' + identifier(file['id']) + '/download' if kind == 'gitlab' else
                       base + '/' + quote(owner, safe='') + '/-/packages/' + package_type + '/' + '/'.join(quote(v, safe='') for v in (name, version)) + '/files/' + identifier(file['id']))
                assets.append({'url': url, 'web': kind == 'gitlab' and package_type != 'generic',
                    'expected_sha256': sha, 'metadata': {'source_id': 'package-file-' + identifier(file['id']),
                    'parent_kind': 'package', 'parent_id': ident, 'name': filename,
                    'size': file['size'], 'created': timestamp(file.get('created_at'), optional=True)}})
            handled.add(path)
        if all_supported:
            handled.add(dataset['path'])
    normalized['pending'] = [row for row in normalized['pending'] if row.get('path') not in handled]
    return assets


def verification_plan(normalized, assets):
    packages = {obj['source_id']: obj['history'] for obj in normalized['objects'] if obj['kind'] == 'package'}
    return [{'package': packages[asset['metadata']['parent_id']], 'file': asset['metadata'],
             'sha256': asset['expected_sha256']} for asset in assets if asset['metadata']['parent_kind'] == 'package']


def verify_published(receiver, repo, plan):
    import hashlib
    import requests
    from svn_migration.common import MigrationError
    owner = quote(repo['owner']['login'], safe='')
    for item in plan:
        package, file = item['package'], item['file']
        relative = '/'.join(quote(value, safe='') for value in (package['name'], package['version']))
        metadata = receiver.api('GET', '/packages/' + owner + '/' + package['type'] + '/' + relative)
        require(metadata.get('repository', {}).get('id') == repo['id'], '原生制品没有关联到本次迁移仓库')
        sha, count = hashlib.sha256(), 0
        if package['type'] == 'generic':
            url = receiver.base + '/api/packages/' + owner + '/generic/' + relative + '/' + quote(file['name'], safe='')
        else:
            rows = receiver.api('GET', '/packages/' + owner + '/' + package['type'] + '/' + relative + '/files')
            target = next((row for row in rows if row['name'] == file['name']), None)
            require(target and target['sha256'] == item['sha256'], '原生制品文件清单与来源不符')
            prefix = receiver.base + '/api/packages/' + owner
            name, version, filename = package['name'], package['version'], quote(file['name'], safe='')
            if package['type'] == 'maven':
                group, artifact = name.split(':', 1)
                suffix = '/maven/' + quote(group.replace('.', '/'), safe='/') + '/' + quote(artifact, safe='') + '/' + quote(version, safe='') + '/' + filename
            elif package['type'] == 'npm':
                suffix = '/npm/' + quote(name, safe='/') + '/-/' + quote(version, safe='') + '/' + filename
            elif package['type'] == 'pypi':
                suffix = '/pypi/files/' + relative + '/' + filename
            elif package['type'] == 'nuget':
                suffix = '/nuget/package/' + quote(name.lower(), safe='') + '/' + quote(version.lower(), safe='') + '/' + filename
            else:
                suffix = '/helm/' + filename
            url = prefix + suffix
        try:
            with receiver.session.get(url, stream=True, timeout=(10, 60), allow_redirects=False, verify=receiver.verify) as response:
                require(response.status_code == 200, '原生制品协议无法下载迁移文件')
                for chunk in response.iter_content(1024 * 1024):
                    receiver.process.tick()
                    count += len(chunk)
                    require(count <= file['size'], '原生制品返回数据超出源文件大小')
                    sha.update(chunk)
        except requests.RequestException:
            raise MigrationError('原生制品下载核验中断，目标仍保持冻结') from None
        require(count == file['size'] and sha.hexdigest() == item['sha256'], '原生制品协议下载摘要与来源不符')
