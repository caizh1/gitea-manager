"""仅在专属模拟主机内保存与恢复停机基线，不依赖待测 Manager。"""
import contextlib
import copy
import hashlib
import json
import re
import secrets
import time

from .errors import CheckFailure, EnvironmentBlocked


def name(value):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', value):
        raise CheckFailure('检查点名称不符合隔离目录约束')
    return value


def available(lab, node):
    return node + ':' + lab.state.get('active_checkpoint_label', 'fault-base') in lab.state.get('checkpoints', {})


def save(lab, node, label='fault-base', resume=True):
    lab.ensure_space()
    label = name(label)
    directory = '/lab/checkpoints/' + label
    if lab.command(node, ['test', '-e', directory + '/complete'], check=False)[0] == 0:
        raise CheckFailure('检查点已经存在，拒绝覆盖首次基线')
    lab.command(node, ['mkdir', '-p', directory])
    runner = lab.command(node, ['docker', 'inspect', '-f', '{{.State.Running}}', 'lab-runner'], check=False)[1].strip() == b'true'
    if runner:
        lab.command(node, ['docker', 'stop', 'lab-runner'])
    completed = False
    try:
        lab.command(node, ['docker', 'compose', '-f', '/lab/compose.json', 'stop', 'gitea', 'postgres'])
        # 禁止跨文件系统或跟随软链接；持久数据本身保留其权限与数字 UID。
        lab.command(node, ['tar', '-cpf', directory + '/persist.tar', '-C', '/lab', 'persist'], timeout=1200)
        external_runner = lab.command(node, ['test', '-d', '/lab/runner'], check=False)[0] == 0
        if external_runner:
            lab.command(node, ['tar', '-cpf', directory + '/runner.tar', '-C', '/lab', 'runner'])
            runner_digest = lab.command(node, ['sha256sum', directory + '/runner.tar'])[1].decode().split()[0]
        else:
            runner_digest = None
        lab.command(node, ['cp', '/lab/compose.json', directory + '/compose.json'])
        digest = lab.command(node, ['sha256sum', directory + '/persist.tar'])[1].decode().split()[0]
        lab.put(node, directory + '/complete', digest + '\n')
        lab.state.setdefault('checkpoints', {})[node + ':' + label] = {
            '节点状态': copy.deepcopy(lab.state['nodes'][node]), 'SHA256': digest,
            'Runner运行': runner, '外部Runner夹具': external_runner,
            'RunnerSHA256': runner_digest,
            '说明': '停止 Gitea 与 PostgreSQL 后由独立测试工具保存的原始持久卷',
            '时间': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        lab.save()
        completed = True
    finally:
        if resume or not completed:
            lab.command(node, ['docker', 'compose', '-f', '/lab/compose.json', 'up', '-d'])
            lab.wait_gitea(node)
            if runner:
                lab.command(node, ['docker', 'start', 'lab-runner'])


def restore(lab, node, label=None, retain=False):
    lab.ensure_space()
    label = label or lab.state.get('active_checkpoint_label', 'fault-base')
    label = name(label)
    saved = lab.state.get('checkpoints', {}).get(node + ':' + label)
    if not saved:
        raise CheckFailure('没有独立检查点，拒绝继续破坏性用例')
    directory = '/lab/checkpoints/' + label
    archive_bytes = int(lab.command(node, ['stat', '-c', '%s', directory + '/persist.tar'])[1])
    free_bytes = int(lab.command(node, ['df', '-Pk', '/lab'])[1].decode().splitlines()[-1].split()[3]) * 1024
    if free_bytes < archive_bytes * 2 + 10 * 1024**3:
        raise EnvironmentBlocked('独立基线接管所需空间不足，保留当前现场并停止，未删除其他任务资源')
    digest = lab.command(node, ['sha256sum', directory + '/persist.tar'])[1].decode().split()[0]
    if digest != saved['SHA256']:
        raise CheckFailure('独立检查点摘要发生变化，保留当前现场')
    if saved.get('外部Runner夹具'):
        actual = lab.command(node, ['sha256sum', directory + '/runner.tar'])[1].decode().split()[0]
        if actual != saved.get('RunnerSHA256'):
            raise CheckFailure('外部 Runner 夹具检查点摘要发生变化，保留当前现场')
    lab.state['baseline_dirty'] = '独立检查点切换已开始；后续静态迁移前必须完整复验并重采'
    lab.save()
    suffix = str(time.time_ns())
    staging, previous = '/lab/restore-' + suffix, '/lab/previous-' + suffix
    lab.command(node, ['mkdir', staging])
    lab.command(node, ['tar', '-xpf', directory + '/persist.tar', '-C', staging], timeout=1200)
    lab.command(node, ['docker', 'stop', 'lab-runner'], check=False)
    lab.command(node, ['docker', 'compose', '-f', '/lab/compose.json', 'stop', 'gitea', 'postgres'], check=False)
    lab.command(node, ['docker', 'rm', '-f', 'gitea', 'postgres'], check=False)
    lab.command(node, ['mv', '/lab/persist', previous])
    try:
        lab.command(node, ['mv', staging + '/persist', '/lab/persist'])
        lab.command(node, ['cp', directory + '/compose.json', '/lab/compose.json'])
        if saved.get('外部Runner夹具'):
            # 这是独立测试基线对外部设施的接管，不是 Manager 备份包含 Runner 主机的证据。
            lab.command(node, ['rm', '-rf', '/lab/runner'])
            lab.command(node, ['tar', '-xpf', directory + '/runner.tar', '-C', '/lab'])
        lab.command(node, ['docker', 'compose', '-f', '/lab/compose.json', 'up', '-d'])
        lab.state['nodes'][node] = copy.deepcopy(saved['节点状态'])
        lab.save()
        lab.wait_gitea(node)
        if saved.get('Runner运行'):
            lab.command(node, ['docker', 'start', 'lab-runner'])
        lab.manager_api('PUT', '/servers/%s' % saved['节点状态']['server_id'],
                        json={'api_token': saved['节点状态']['token']})
    except Exception:
        # 不自动反复切换数据，目录名和首次错误保留用于人工接管。
        lab.note('checkpoint', '独立基线接管失败', '失败', '当前现场保留：' + previous)
        raise
    else:
        if not retain:
            lab.command(node, ['rm', '-rf', previous], timeout=1200)
        lab.command(node, ['rmdir', staging])


def prepare(lab):
    from . import oracle
    lab.login()
    label = lab.state.get('active_checkpoint_label', 'fault-base')
    for node in ('a', 'b'):
        with lab.case('checkpoint', node + '：独立停机基线'):
            oracle.settle(lab, node)
            before = oracle.snapshot(lab, node)
            if not available(lab, node):
                save(lab, node, label=label)
            else:
                lab.note('checkpoint-reuse', node + '：保留既有检查点', '通过', '不覆盖原始归档；先核验摘要，再用当前完整基线验证往返')
            restore(lab, node)
            after = oracle.snapshot(lab, node)
            differences = oracle.compare(before, after)
            path = lab.root / ('checkpoint-comparison-' + node + '-' + str(time.time_ns()) + '.json')
            path.write_text(json.dumps({'说明': '独立检查点往返，保留全部原始快照及差异',
                '之前快照': before['证据文件'], '之后快照': after['证据文件'], '差异': differences}, ensure_ascii=False, indent=2) + '\n')
            path.chmod(0o600)
            if differences:
                raise CheckFailure('独立基线往返不一致：' + json.dumps(differences, ensure_ascii=False))


def full_roundtrip(lab):
    """保留旧检查点；真实触发原生审计追加，并核验全部表的独立停机往返。"""
    from . import oracle
    from .audit_compare import recovery_append_only
    lab.login()
    with lab.case('checkpoint-full-preflight', '全表检查点两份永久归档及往返临时副本空间'):
        # 两台主机的新检查点都会保留；不能仅检查一次恢复的瞬时预算。
        lab.ensure_space(future_backups=2)
    label = 'full-table-' + str(time.time_ns())
    for node in ('a', 'b'):
        with lab.case('checkpoint-full', node + '：全表往返与原生审计追加'):
            lab.ensure_space()
            oracle.settle(lab, node)
            before = oracle.snapshot(lab, node)
            operations = [row for row in before['业务表'].get('governance_repository_creation', [])
                          if row['state'] == 'unknown' and row['last_reason_code'] == 'completion_marker_missing']
            if not operations:
                raise CheckFailure('没有既存 unknown 核对样本，不能将无追加当作正向分支通过')
            operation = operations[0]
            # 源码确认该字段只控制下次原生核对；停服后调整，避免保存前已追加。
            lab.command(node, ['docker', 'stop', 'gitea'])
            try:
                identifier = "'" + operation['id'].replace("'", "''") + "'"
                oracle.sql(lab, node, 'UPDATE governance_repository_creation SET next_attempt_unix=0 WHERE id=' + identifier)
                save(lab, node, label=label, resume=False)
                restore(lab, node, label=label)
            finally:
                lab.command(node, ['docker', 'compose', '-f', '/lab/compose.json', 'up', '-d'])
                lab.wait_gitea(node)
            deadline = time.monotonic() + 90
            original_ids = {row['event_id'] for row in before['业务表']['governance_audit_event']}
            while True:
                count = int(oracle.sql(lab, node, "SELECT count(*) FROM governance_audit_event WHERE type='repository.creation_recovery_required'"))
                old_count = sum(row['type'] == 'repository.creation_recovery_required'
                                for row in before['业务表']['governance_audit_event'])
                if count > old_count:
                    break
                if time.monotonic() > deadline:
                    raise CheckFailure('恢复后未观察到原生核对审计追加，不能宣称规则正向分支已验证')
                time.sleep(1)
            after = oracle.snapshot(lab, node)
            added = [row['event_id'] for row in after['业务表']['governance_audit_event'] if row['event_id'] not in original_ids]
            differences = oracle.compare(before, after)
            valid_append = bool(added) and recovery_append_only(before['业务表'], after['业务表'])
            path = lab.root / ('checkpoint-full-' + node + '-' + str(time.time_ns()) + '.json')
            path.write_text(json.dumps({'说明': '完整表独立往返；只调整既存 unknown 操作下次核对时间，由原生启动任务追加审计',
                '检查点': label, '之前快照': before['证据文件'], '之后快照': after['证据文件'],
                '数据表数': len(after['业务表']), '原审计数': len(original_ids), '追加事件': added,
                '追加规则通过': valid_append, '差异': differences}, ensure_ascii=False, indent=2) + '\n')
            path.chmod(0o600)
            if differences or not valid_append:
                raise CheckFailure('全表往返或原生审计追加不符合精确规则，详见 ' + path.name)
    lab.state['active_checkpoint_label'] = label
    lab.save()


@contextlib.contextmanager
def empty_target(lab, node='b', postgres_image=None):
    """临时部署有效的空白目标；退出时恢复原卷，失败的新卷保留。"""
    if node != 'b':
        raise CheckFailure('空白目标夹具只允许操作本轮 B 节点')
    lab.ensure_space()
    original_state = copy.deepcopy(lab.state['nodes'][node])
    original = lab.command(node, ['cat', '/lab/compose.json'])[1]
    config = lab.compose_config(node, original)
    if postgres_image:
        if postgres_image != lab.state.get('pg_variant_image'):
            raise CheckFailure('PG 变体未经过本轮镜像身份核对')
        config['services']['postgres']['image'] = postgres_image
    prefix = '/lab/empty-target-' + str(time.time_ns())
    runner = lab.command(node, ['docker', 'inspect', '-f', '{{.State.Running}}', 'lab-runner'], check=False)[1].strip() == b'true'
    lab.command(node, ['docker', 'stop', 'lab-runner'], check=False)
    lab.command(node, ['docker', 'compose', '-f', '/lab/compose.json', 'stop', 'gitea', 'postgres'])
    lab.command(node, ['docker', 'rm', '-f', 'gitea', 'postgres'])
    lab.command(node, ['mv', '/lab/persist', prefix + '-original'])
    failed = True
    try:
        lab.command(node, ['mkdir', '-p', '/lab/persist/gitea'])
        # SSH 主机身份属于目标部署参数，必须在数据迁移前后保持不变。
        lab.command(node, ['cp', '-a', prefix + '-original/gitea/ssh', '/lab/persist/gitea/ssh'])
        environment = config['services']['gitea']['environment']
        for key in ('GITEA__security__SECRET_KEY', 'GITEA__security__SECRET_KEY_URI', 'GITEA__security__SECRET_KEY__FILE'):
            environment.pop(key, None)
        new_key = secrets.token_urlsafe(32)
        environment['GITEA__security__SECRET_KEY'] = new_key
        lab.state['nodes'][node].update(secret=new_key, token='')
        lab.save()
        lab.put(node, '/lab/compose.json', json.dumps(config))
        lab.command(node, ['docker', 'compose', '-f', '/lab/compose.json', 'up', '-d'])
        lab.wait_gitea(node)
        lab.command(node, ['docker', 'exec', '-u', 'git', 'gitea', 'gitea', 'admin', 'user', 'create',
            '--username', 'labadmin', '--password', original_state['password'], '--email', 'admin@lab.invalid',
            '--admin', '--must-change-password=false'])
        token = lab.command(node, ['docker', 'exec', '-u', 'git', 'gitea', 'gitea', 'admin', 'user', 'generate-access-token',
            '--username', 'labadmin', '--token-name', 'empty-target', '--scopes', 'all', '--raw'])[1].decode().strip().splitlines()[-1]
        lab.state['nodes'][node]['token'] = token
        lab.save()
        lab.api(node, 'GET', '/user')
        lab.manager_api('PUT', '/servers/%s' % original_state['server_id'], json={'api_token': token})
        yield
        failed = False
    finally:
        lab.command(node, ['docker', 'compose', '-f', '/lab/compose.json', 'stop', 'gitea', 'postgres'], check=False)
        lab.command(node, ['docker', 'rm', '-f', 'gitea', 'postgres'], check=False)
        has_result = lab.command(node, ['test', '-d', '/lab/persist'], check=False)[0] == 0
        if has_result:
            lab.command(node, ['mv', '/lab/persist', prefix + '-result'])
        lab.command(node, ['mv', prefix + '-original', '/lab/persist'])
        lab.put(node, '/lab/compose.json', original)
        lab.state['nodes'][node] = original_state
        lab.save()
        lab.command(node, ['docker', 'compose', '-f', '/lab/compose.json', 'up', '-d'])
        lab.wait_gitea(node)
        lab.manager_api('PUT', '/servers/%s' % original_state['server_id'], json={'api_token': original_state['token']})
        if runner:
            lab.command(node, ['docker', 'start', 'lab-runner'])
        if failed and has_result:
            lab.note('fixture-evidence', '保留空白目标失败现场', '环境阻塞', prefix + '-result')
        elif has_result:
            lab.command(node, ['rm', '-rf', prefix + '-result'], timeout=1200)
