"""以测试专属 tmpfs 注入容量及 inode 故障，不填满宿主或现有卷。"""
import contextlib
import hashlib
import json
import time

from .errors import CheckFailure


def parse_stop_events(raw, identifier, started, ended):
    """兼容经典及新版 Docker 事件结构，仍严格限定实际源容器和任务时间窗。"""
    events = []
    for row in (json.loads(line) for line in raw.decode().splitlines()):
        actor = row.get('Actor', {}).get('ID', row.get('id'))
        action = row.get('Action', row.get('status'))
        timestamp = row['timeNano']
        if actor == identifier and action == 'stop' and started <= timestamp / 1e9 <= ended:
            events.append({'容器ID': actor, '事件': action, '纳秒': timestamp})
    return events


def stop_evidence(lab, started, ended, observed):
    """容器 stop 事件独立证明停源；认证状态未知不能当成停机或预检通过。"""
    identifier = lab.command('a', ['docker', 'inspect', '--format', '{{.Id}}', 'gitea'])[1].decode().strip()
    raw = lab.command('a', ['docker', 'events', '--since', str(int(started)), '--until', str(int(ended) + 1),
        '--filter', 'container=' + identifier, '--filter', 'event=stop', '--format', '{{json .}}'])[1]
    path = lab.root / ('source-stop-events-' + str(time.time_ns()) + '.json')
    path.write_text(json.dumps({'说明': '源任务区间实际 Docker stop 事件原文，解析前保存',
        '原文': raw.decode(), '源容器ID': identifier, '开始墙钟秒': started, '结束墙钟秒': ended}, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)
    events = parse_stop_events(raw, identifier, started, ended)
    samples = [row for row in observed if started <= row.get('墙钟秒', 0) <= ended]
    return {'源停容器事件': events, '任务区间源不可达样本数': sum(row['a'] is False for row in samples),
            '任务区间源未确认样本数': sum(row['a'] is None for row in samples), '任务区间样本数': len(samples),
            '探针版本': 2, '任务区间开始墙钟秒': started, '任务区间结束墙钟秒': ended}


def preflight_result(proof):
    if proof['源停容器事件'] or proof['任务区间源不可达样本数']:
        return '失败', '已知不可写时仍停止源；安全恢复不能抵消预检失败'
    if not proof['任务区间样本数'] or proof['任务区间源未确认样本数']:
        raise CheckFailure('预检可用性证据不完整，不能判定停服前拒绝通过')
    return '通过', '未观察到源停容器事件或认证服务中断'


@contextlib.contextmanager
def source_tmpfs(lab, options):
    with lab.ssh('a') as client:
        with client.open_sftp() as sftp:
            with sftp.file('/lab/compose.json', 'rb') as stream:
                original = stream.read()
    config = lab.compose_config('a', original)
    volume = 'fault-tmp-v2-' + hashlib.sha256(options.encode()).hexdigest()[:8]
    # 先让 Docker 在可写卷上完成初始化权限，再以只读挂载交给容器。
    config.setdefault('volumes', {})[volume] = {'driver': 'local', 'driver_opts': {'type': 'tmpfs', 'device': 'tmpfs', 'o': options.removeprefix('ro,')}}
    config['services']['gitea']['volumes'].append({'type': 'volume', 'source': volume, 'target': '/tmp',
        'read_only': options.startswith('ro,'), 'volume': {'nocopy': True}})
    lab.put('a', '/lab/compose.json', json.dumps(config))
    try:
        lab.command('a', ['docker', 'compose', '-f', '/lab/compose.json', 'up', '-d', '--force-recreate', 'gitea'])
        lab.wait_gitea('a')
        yield
    finally:
        lab.put('a', '/lab/compose.json', original)
        lab.command('a', ['docker', 'compose', '-f', '/lab/compose.json', 'up', '-d', '--force-recreate', 'gitea'])
        lab.wait_gitea('a')


def failures(lab):
    source_failures(lab)
    manager_failures(lab)


def source_failures(lab, modes=None):
    from . import oracle
    lab.login()
    for label, options in [('源临时盘容量不足', 'size=16m,mode=1777'),
                           ('源临时盘 inode 耗尽', 'size=128m,nr_inodes=128,mode=1777'),
                           ('源临时盘只读', 'ro,size=16m,mode=1777'),
                           ('源临时盘缺少写权限', 'size=16m,mode=0555')]:
        if modes is not None and options not in modes:
            continue
        with lab.case('storage-partial', label + '：安全失败与服务恢复'):
            lab.ensure_space()
            baseline = oracle.snapshot(lab, 'a')
            before = lab.command('a', ['docker', 'ps', '-aq', '--filter', 'label=gitea.manager.operation'])[1].decode().splitlines()
            with lab.observe('storage-source-' + hashlib.sha256(options.encode()).hexdigest()[:8]) as observed, source_tmpfs(lab, options):
                proof = {'说明': label, '实际选项': options,
                         '容量': lab.command('a', ['docker', 'exec', 'gitea', 'df', '-Pk', '/tmp'])[1].decode(),
                         'inode': lab.command('a', ['docker', 'exec', 'gitea', 'df', '-Pi', '/tmp'])[1].decode()}
                if 'nr_inodes' in options:
                    lab.command('a', ['docker', 'exec', 'gitea', 'sh', '-c',
                        'i=0; while touch /tmp/inode-$i 2>/dev/null; do i=$((i+1)); done'])
                    row = lab.command('a', ['docker', 'exec', 'gitea', 'df', '-Pi', '/tmp'])[1].decode().splitlines()[-1].split()
                    if int(row[3]) != 0:
                        raise CheckFailure('inode 注入没有实际耗尽，不能记录为通过')
                    proof['inode耗尽后'] = row
                if options.startswith('ro,') or 'mode=0555' in options:
                    code, _, error = lab.command('a', ['docker', 'exec', '-u', 'git', 'gitea',
                        'sh', '-ec', ': > /tmp/remote-lab-write-proof'], check=False)
                    proof.update(写入探针退出码=code, 写入探针错误=error.decode(errors='replace'))
                    if code == 0:
                        raise CheckFailure('只读或权限夹具没有实际拒绝写入')
                started_wall = time.time()
                task = lab.manager_api('POST', '/backups', json={'source_server_id': lab.state['nodes']['a']['server_id']}).json()
                result = lab.poll_task('backup', task['id'])
                ended_wall = time.time()
                proof.update(stop_evidence(lab, started_wall, ended_wall, observed))
                proof.update(任务ID=task['id'], 任务状态=result['status'], 错误=lab.scrub(result.get('error_msg', '')))
                path = lab.root / ('storage-source-' + str(task['id']) + '.json')
                path.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n'); path.chmod(0o600)
                if result['status'] != 'failed' or not result.get('error_msg'):
                    raise CheckFailure('临时盘资源不足未让备份明确失败')
            after = lab.command('a', ['docker', 'ps', '-aq', '--filter', 'label=gitea.manager.operation'])[1].decode().splitlines()
            if before != after:
                raise CheckFailure('临时盘失败后辅助容器未清理')
            code = lab.command('a', ['docker', 'exec', 'gitea', 'test', '!', '-e', '/data/.manager-restore-lock'], check=False)[0]
            if code:
                raise CheckFailure('临时盘失败后留下源实例锁')
            if oracle.compare(baseline, oracle.snapshot(lab, 'a')):
                raise CheckFailure('源临时盘故障后全部业务数据存在差异')
        if proof.get('写入探针退出码'):
            status, detail = preflight_result(proof)
            lab.note('storage-preflight-partial', label + '：停服前拒绝', status, detail,
                任务ID=task['id'], 源停容器事件数=len(proof['源停容器事件']), 源不可达样本数=proof['任务区间源不可达样本数'])


def manager_failures(lab, modes=None):
    """用有界 tmpfs 注入 Manager 故障，绝不填满宿主或遮蔽数据库。"""
    from . import oracle, manager_canaries
    from .network import control
    lab.login()
    for label, mode in [('Manager 备份盘容量不足', 'backup-full'),
                        ('Manager 备份盘 inode 耗尽', 'backup-inodes'),
                        ('Manager 备份盘只读', 'backup-readonly'),
                        ('Manager 临时盘容量不足', 'temporary-full')]:
        if modes is not None and mode not in modes:
            continue
        with lab.case('storage-partial', label + '：安全失败与服务恢复'):
            lab.ensure_space()
            before = oracle.snapshot(lab, 'a')
            observed = []
            started_wall = ended_wall = None
            try:
                proof = control(lab, 'manager-storage', mode=mode, enabled=True)
                lab.login()
                with lab.observe('storage-manager-' + mode) as observed:
                    started_wall = time.time()
                    task = lab.manager_api('POST', '/backups', json={'source_server_id': lab.state['nodes']['a']['server_id']}).json()
                    result = lab.poll_task('backup', task['id'], timeout=900)
                    ended_wall = time.time()
                evidence = {'说明': label, '故障证据': proof, '任务ID': task['id'], '任务状态': result['status'],
                            '错误': lab.scrub(result.get('error_msg', '')),
                            **stop_evidence(lab, started_wall, ended_wall, observed)}
                path = lab.root / ('storage-manager-' + mode + '-' + str(task['id']) + '.json')
                path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
                path.chmod(0o600)
                if result['status'] != 'failed' or not result.get('error_msg'):
                    raise CheckFailure('Manager 存储不足未明确失败，不能保留可用假备份')
            finally:
                control(lab, 'manager-storage', mode=mode, enabled=False)
                lab.login()
            lab.wait_gitea('a')
            manager_canaries.verify(lab)
            if oracle.compare(before, oracle.snapshot(lab, 'a')):
                raise CheckFailure('Manager 存储故障后源业务存在差异')
            if lab.command('a', ['docker', 'exec', 'gitea', 'test', '-e', '/data/.manager-restore-lock'], check=False)[0] == 0:
                raise CheckFailure('Manager 存储故障后留下源锁')
        if mode in ('backup-inodes', 'backup-readonly'):
            status, detail = preflight_result(evidence)
            lab.note('storage-preflight-partial', label + '：停源前拒绝', status, detail,
                任务ID=task['id'], 源停容器事件数=len(evidence['源停容器事件']), 源不可达样本数=evidence['任务区间源不可达样本数'])


def preflight_recheck(lab):
    """修正认证测量后重跑原不可写场景；不减少真实数据和安全断言。"""
    source_failures(lab, {'ro,size=16m,mode=1777', 'size=16m,mode=0555'})
    manager_failures(lab, {'backup-inodes', 'backup-readonly'})
