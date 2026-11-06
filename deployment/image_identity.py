"""从同一离线归档验证配置摘要与 OCI 摘要之间的关系，不依赖 Docker 存储实现。"""
import hashlib
import json
import re
import tarfile


def archive_image_ids(path, images):
    """只返回与交付清单及 Linux amd64 配置相连的归档身份。"""
    with tarfile.open(path) as archive:
        def read_json(name):
            member = archive.getmember(name)
            if not member.isfile() or member.size > 4 * 1024 * 1024:
                raise RuntimeError('离线镜像元数据不是普通文件或超过大小限制：' + name)
            raw = archive.extractfile(member).read()
            return raw, json.loads(raw)

        _, records = read_json('manifest.json')
        try:
            _, index = read_json('index.json')
        except KeyError:
            index = {'manifests': []}

        result = {}
        for key, item in images.items():
            tag = item['tag']
            rows = [row for row in records if tag in (row.get('RepoTags') or [])]
            if len(rows) != 1:
                raise RuntimeError('离线归档中的镜像标签缺失或重复：' + tag)
            raw, config = read_json(rows[0]['Config'])
            config_id = 'sha256:' + hashlib.sha256(raw).hexdigest()
            if config.get('architecture') != 'amd64' or config.get('os') != 'linux':
                raise RuntimeError('离线归档镜像架构不符合 Linux amd64：' + tag)
            identities = {config_id}

            def canonical(name):
                # Docker 默认仓库的几种等价写法；不按后缀匹配其他命名空间。
                if name.startswith('index.docker.io/'):
                    name = 'docker.io/' + name[len('index.docker.io/'):]
                first = name.split('/')[0]
                if '/' not in name or ('.' not in first and ':' not in first and first != 'localhost'):
                    name = 'docker.io/' + name
                if name.startswith('docker.io/') and '/' not in name[len('docker.io/'):]:
                    name = 'docker.io/library/' + name[len('docker.io/'):]
                return name

            descriptors = [entry for entry in index.get('manifests', [])
                           if canonical(entry.get('annotations', {}).get('io.containerd.image.name', '')) == canonical(tag)]
            if len(descriptors) > 1:
                raise RuntimeError('离线归档中的 OCI 标签重复：' + tag)
            visited = set()

            def connected(descriptor, depth=0):
                digest = descriptor.get('digest', '')
                if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest) or depth > 8 or digest in visited or len(visited) >= 64:
                    raise RuntimeError('离线镜像 OCI 摘要或引用层级无效：' + tag)
                visited.add(digest)
                data, value = read_json('blobs/sha256/' + digest.split(':')[1])
                if 'sha256:' + hashlib.sha256(data).hexdigest() != digest or descriptor.get('size') != len(data):
                    raise RuntimeError('离线镜像 OCI 元数据摘要不一致：' + tag)
                media = descriptor.get('mediaType')
                if media in ('application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json'):
                    found = set()
                    for child in value.get('manifests', []):
                        platform = child.get('platform', {})
                        if platform and (platform.get('os') != 'linux' or platform.get('architecture') != 'amd64'):
                            continue
                        found.update(connected(child, depth + 1))
                    return found | {digest} if found else set()
                if media in ('application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'):
                    if value.get('config', {}).get('digest') == config_id and value['config'].get('size') == len(raw):
                        return {digest}
                    return set()
                raise RuntimeError('离线镜像 OCI 元数据类型不支持：' + tag)

            if descriptors:
                linked = connected(descriptors[0])
                if not linked:
                    raise RuntimeError('离线镜像 OCI 清单没有关联交付配置：' + tag)
                identities.update(linked)
            if not identities.intersection(item['ids']):
                raise RuntimeError('离线镜像与交付清单没有相同身份：' + tag)
            result[key] = sorted(identities)
        return result
