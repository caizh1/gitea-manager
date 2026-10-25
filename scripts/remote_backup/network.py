"""通过带白名单的宿主控制通道注入网络故障；业务访问仍经过路由器。"""
import json
import secrets
import socket
import time

import requests

from .errors import CheckFailure

VARIANTS = [('both', seconds) for seconds in (5, 30, 180, 600)]
VARIANTS += [(mode, 30) for mode in ('to-node', 'from-node', 'reset', 'ssh-only', 'api-only')]


def retained_archives(repeats=2, include_stable=True):
    """两种任务、九种故障各重复两轮；每例原任务和解除后的新流程各保留归档。"""
    if type(repeats) is not int or repeats < 1:
        raise ValueError('故障重复次数必须为正整数')
    return (2 if include_stable else 0) + len(VARIANTS) * 2 * repeats * 2


def matrix_preflight(lab):
    lab.login()
    with lab.case('network-preflight', '完整网络两轮矩阵全部新增归档空间'):
        for endpoint in ('/backups', '/restore-tasks'):
            if any(row['status'] in ('running', 'pending') for row in lab.manager_api('GET', endpoint).json()):
                raise CheckFailure('已有活动备份恢复，尚未修改网络或实例')
        lab.ensure_space(future_backups=retained_archives())


def control(lab, operation, *, wait=True, **values):
    directory = lab.root / 'control'
    directory.mkdir(exist_ok=True)
    identifier = str(time.time_ns()) + '-' + secrets.token_hex(4)
    path = directory / (identifier + '.request.json')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'说明': '仅影响本轮隔离环境', 'operation': operation, **values}, ensure_ascii=False))
    temporary.chmod(0o600)
    temporary.replace(path)
    response = directory / (identifier + '.response.json')
    if not wait:
        return response
    return wait_control(response)


def wait_control(path, timeout=900):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise CheckFailure('独立故障控制器未响应，不能算作故障注入成功')
        time.sleep(0.2)
    result = json.loads(path.read_text())
    if result['状态'] != '完成':
        raise CheckFailure('独立故障控制器失败：' + result.get('说明', ''))
    return result


def wait_stage(lab, kind, identifier, stages, timeout=3600):
    deadline = time.monotonic() + timeout
    endpoint = '/backups' if kind == 'backup' else '/restore-tasks'
    while time.monotonic() < deadline:
        rows = lab.manager_api('GET', endpoint).json()
        task = next(row for row in rows if row['id'] == identifier)
        if task.get('progress_stage') in stages:
            return task
        if task['status'] != 'running':
            raise CheckFailure('任务在目标故障阶段之前结束，本次没有命中故障点')
        time.sleep(0.25)
    raise CheckFailure('未观察到待注入的任务阶段')


def stable_profiles(lab):
    from .oracle import snapshot, compare
    from .helper_preservation import snapshot as helper_snapshot, compare as helper_compare
    from .manager_canaries import verify
    from .scenarios import write_json
    lab.login()
    with lab.case('network-stable-preflight', '两份劣化链路归档保留及恢复峰值空间'):
        for endpoint in ('/backups', '/restore-tasks'):
            if any(row['status'] in ('running', 'pending') for row in lab.manager_api('GET', endpoint).json()):
                raise CheckFailure('已有活动备份恢复，尚未修改链路')
        lab.ensure_space(future_backups=2)
        retained_helpers = {node: helper_snapshot(lab, node) for node in ('a', 'b')}
        verify(lab)
    baseline = snapshot(lab, 'a')
    lab.state['baseline_dirty'] = '真实劣化链路恢复已开始，静态迁移前必须完整复验并重采'
    lab.save()
    owner = secrets.token_hex(16)
    for profile in ('wan', 'lossy'):
        with lab.case('network-stable', '完整数据集网络剖面传输及数据核验：' + profile):
            path = lab.root / ('network-stable-' + profile + '-' + str(time.time_ns()) + '.json')
            proof = {'说明': '真实路由器劣化下完整备份恢复；配置差异单列失败，不代表完整迁移通过',
                     '剖面': profile, '之前源快照': baseline['证据文件']}
            try:
                applied = control(lab, 'profile', profile=profile, profile_owner=owner)
                proof['剖面应用证据'] = applied; write_json(path, proof)
                backup = lab.backup('a')
                proof['备份ID'] = backup['id']; write_json(path, proof)
                result = lab.restore('b')
                proof['恢复任务'] = result; write_json(path, proof)
                if result['status'] != 'success':
                    raise CheckFailure('网络劣化后恢复失败：' + result.get('error_msg', ''))
                target, source = snapshot(lab, 'b'), snapshot(lab, 'a')
                proof.update(之后源快照=source['证据文件'], 之后目标快照=target['证据文件'])
                differences = compare(baseline, target)
                source_changes = compare(baseline, source)
                proof.update(目标差异=differences, 源差异=source_changes); write_json(path, proof)
                if source_changes:
                    raise CheckFailure('劣化链路完整流程后源业务变化：' + json.dumps(source_changes, ensure_ascii=False))
                settings = [item for item in differences if item['范围'] == '业务配置']
                if settings:
                    lab.note('settings', profile + '：网络劣化后的业务配置', '失败', json.dumps(settings, ensure_ascii=False))
                changes = [item for item in differences if item['范围'] != '业务配置']
                if changes:
                    raise CheckFailure('劣化链路下数据不一致：' + json.dumps(changes, ensure_ascii=False))
                for node in ('a', 'b'):
                    helper_compare(retained_helpers[node], helper_snapshot(lab, node))
                verify(lab)
                proof['数据核验'] = '通过'; proof['完整迁移'] = '失败' if settings else '通过'
                write_json(path, proof)
            finally:
                proof['剖面还原证据'] = control(lab, 'profile', profile='normal', profile_owner=owner)
                write_json(path, proof)


def disruptions(lab, repeats=2):
    from . import checkpoint, oracle
    from .scenarios import compare_target, verify_business
    lab.login()
    completed = []
    with lab.case('network-interruption-preflight', '所有中断两轮归档保留的整阶段空间'):
        for endpoint in ('/backups', '/restore-tasks'):
            if any(row['status'] in ('running', 'pending') for row in lab.manager_api('GET', endpoint).json()):
                raise CheckFailure('已有活动备份恢复，尚未切换检查点或断网')
        lab.ensure_space(future_backups=retained_archives(repeats, include_stable=False))
    if not all(checkpoint.available(lab, node) for node in ('a', 'b')):
        raise CheckFailure('网络破坏测试需要两侧独立停机检查点')
    kinds = [('backup', 'a', {'transfer'}), ('restore', 'b', {'transfer_files', 'transfer_database'})]
    runs = [(repeat, kind, node, stages, mode, duration) for repeat in range(1, repeats + 1)
            for kind, node, stages in kinds for mode, duration in VARIANTS]
    for repeat, kind, node, stages, mode, duration in runs:
        case = '%s-transfer-%s-%ss-round-%s' % (kind, mode, duration, repeat)
        with lab.case('network-interruption', case):
            lab.ensure_space(future_backups=3)
            for reset_node in ('a', 'b'):
                checkpoint.restore(lab, reset_node)
            before = oracle.snapshot(lab, 'a')
            if kind == 'restore':
                lab.backup('a')
            if kind == 'backup':
                response = lab.manager_api('POST', '/backups', json={'source_server_id': lab.state['nodes'][node]['server_id']}).json()
            else:
                response = lab.manager_api('POST', '/restore', json={'backup_id': lab.state['backup_id'],
                    'target_server_id': lab.state['nodes'][node]['server_id']}).json()
            task_id = response['id']
            task = wait_stage(lab, kind, task_id, stages)
            control_file = control(lab, 'disconnect', node=node, seconds=duration, mode=mode, wait=False)
            active = control_file.with_suffix('.active')
            deadline = time.monotonic() + 15
            while not active.exists() and not control_file.exists():
                if time.monotonic() > deadline:
                    raise CheckFailure('控制器未确认实际断网')
                time.sleep(0.1)
            ssh_up = False
            try:
                with socket.create_connection((lab.state['addresses'][node], 22), timeout=1):
                    ssh_up = True
            except (TimeoutError, ConnectionError, OSError):
                pass
            if ssh_up != (mode == 'api-only'):
                raise CheckFailure('实际 SSH 可达性与注入模式不符，不能认定命中故障')
            if mode in ('ssh-only', 'api-only'):
                try:
                    api_up = lab.request(node, 'GET', '/api/v1/version', expected=None, timeout=1).status_code == 200
                except requests.RequestException:
                    api_up = False
                if api_up != (mode == 'ssh-only'):
                    raise CheckFailure('独立 API/SSH 故障没有实际隔离两个端口')
            injected = wait_control(control_file)
            result = lab.poll_task(kind, task_id, timeout=1800)
            if result['status'] not in ('failed', 'success'):
                raise CheckFailure('网络恢复后任务仍没有明确终态')
            if result['status'] == 'failed' and not result.get('error_msg'):
                raise CheckFailure('失败没有可见原因')
            # 真实源服务恢复及锁释放必须观察，不能由测试代码静默清锁。
            lab.wait_gitea('a')
            lock = lab.command('a', ['docker', 'exec', 'gitea', 'test', '!', '-e', '/data/.manager-restore-lock'], check=False)[0]
            if lock:
                raise CheckFailure('断网后源实例锁仍残留；保留现场，停止后续破坏测试')
            if kind == 'restore' and result['status'] == 'failed':
                recovery = lab.manager_api('POST', '/restore-tasks/%s/recover' % task_id, expected=None)
                # 无恢复日志时允许接口明确拒绝；后续健康与新恢复仍必须通过。
                if recovery.status_code not in (200, 409):
                    raise CheckFailure('恢复接管接口返回异常状态')
            if result['status'] == 'success' and kind == 'restore':
                lab.adopt_identity('b', 'a')
                changes = compare_target(lab, before, 'b', case + '-initial')
                if any(change['范围'] != '业务配置' for change in changes):
                    raise CheckFailure('故障期间宣称成功的恢复存在数据差异')
            source_changes = oracle.compare(before, oracle.snapshot(lab, 'a'))
            if source_changes:
                raise CheckFailure('网络故障改变了源业务数据：' + json.dumps(source_changes, ensure_ascii=False))
            lab.backup('a')
            rerun = lab.restore('b')
            if rerun['status'] != 'success':
                raise CheckFailure('解除故障后的完整复验失败')
            changes = compare_target(lab, before, 'b', case + '-retry')
            if any(change['范围'] != '业务配置' for change in changes) or not verify_business(lab, 'b', case):
                raise CheckFailure('解除网络故障后的独立数据或实际业务检查失败')
            completed.append({'故障': case, '注入阶段': task.get('progress_stage'), '任务': task_id,
                              '实际终态': result['status'], '控制证据': injected['证据']})
            (lab.root / 'network-disruptions.json').write_text(json.dumps({'说明': '逐例追加实际故障证据', '结果': completed}, ensure_ascii=False, indent=2) + '\n')
    (lab.root / 'network-disruptions.json').write_text(json.dumps({'说明': '实际跨路由器中断证据', '结果': completed}, ensure_ascii=False, indent=2) + '\n')
    lab.note('network-interruption', '十八组方向、复位和独立端口故障各两轮', '通过', '各中断均观察终态、源服务、独立数据及故障解除后的实际业务复验')
