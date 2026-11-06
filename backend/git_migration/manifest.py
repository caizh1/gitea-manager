"""协议八固定清单：与接收端共享长度前缀编码，恢复不重建封存基线。"""
import hashlib
import struct
from svn_migration.common import require


def hash_fields(values):
    result = hashlib.sha256()
    for value in values:
        raw = str(value).encode('utf-8')
        result.update(struct.pack('>Q', len(raw)))
        result.update(raw)
    return result.hexdigest()


def metadata_digest(value):
    return hash_fields(['gitea-migration-asset-v1', *[value[k] for k in
        ('source_id', 'name', 'parent_kind', 'parent_id', 'sha256', 'size', 'created')]])


def summary(checkpoint):
    objects = sorted(checkpoint.get('objects', {}).values(), key=lambda r: r['id'])
    assets = sorted(checkpoint.get('assets', {}).values(), key=lambda r: r['id'])
    fields = ['gitea-migration-manifest-v1', len(objects)]
    for row in objects:
        fields.extend(row[k] for k in ('id', 'kind', 'source_id', 'target_id', 'digest'))
    fields.append(len(assets))
    for row in assets:
        fields.extend(row[k] for k in ('id', 'source_id', 'parent_kind', 'parent_id', 'metadata_digest', 'size', 'sha256', 'uuid', 'attachment_id'))
    return {'version': 1, 'objects': len(objects), 'assets': len(assets), 'digest': hash_fields(fields)}


def verify(receiver, checkpoint):
    expected = checkpoint.get('seal_intent')
    require(expected and summary(checkpoint) == expected, '本地封存清单缺失或已变化', 'manifest')
    actual = receiver.api('GET', '/governance/migration-sessions/' + checkpoint['key'] + '/manifest')
    require(all(actual.get(k) == v for k, v in expected.items()), '接收端完整回执清单与封存基线不符', 'manifest')
    native = checkpoint.get('native_manifest')
    if native:
        require(all(actual.get(k) == v for k, v in native.items()), '目标原生制品清单已变化', 'manifest')
    return actual


def recover(receiver, checkpoint, batch_key, actor, save):
    if not checkpoint.get('session_id'):
        return
    status = receiver.api('GET', '/governance/migration-sessions/' + checkpoint['key'] + '/status')
    require(status.get('id') == checkpoint['session_id'] and status.get('key') == checkpoint['key']
        and status.get('repo_id') == checkpoint['repo_id'] and status.get('batch_key') == batch_key
        and status.get('actor_id') == actor['id'] and status.get('source_digest') == checkpoint.get('source_digest')
        and status.get('publish_barrier') is True, '接收会话身份与固定计划不符', 'identity')
    batch = receiver.api('GET', '/governance/migration-batches/' + batch_key)
    require(batch.get('key') == batch_key and batch.get('actor_id') == actor['id']
        and batch.get('expected') == checkpoint['batch_size'] and batch.get('state') in ('preparing', 'prepared'),
        '预检批次已发布、终止或身份不符', 'identity')
    state = status.get('state')
    require(state in ('loading', 'sealed', 'handed_off'), '接收会话不能继续导入或交接', 'identity')
    if state == 'loading':
        require(not checkpoint.get('sealed') and not checkpoint.get('prepared'), '本地封存状态与接收端冲突', 'manifest')
        return
    expected = checkpoint.get('seal_intent') or {}
    require(status.get('seal_manifest_version') == 1 and status.get('seal_manifest_digest') == expected.get('digest'),
        '接收端封存基线与本地意图不符', 'manifest')
    verify(receiver, checkpoint)
    receiver.verify_objects(checkpoint['key'], checkpoint, lambda _: None)
    receiver.verify_assets(checkpoint)
    checkpoint['sealed'] = True
    save()
