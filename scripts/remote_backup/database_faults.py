"""在独占 PostgreSQL 容器内注入可恢复故障，不触碰其他数据库。"""
import json
import time

from .driver import CheckFailure
from . import oracle


def run(lab):
    source_failures(lab)
    restore_interruption(lab)


def source_failures(lab):
    lab.login()
    for label, extra_client in [('停止源 PostgreSQL', False), ('停止 Gitea 后仍有额外数据库客户端', True)]:
        with lab.case('database-partial', label):
            before = oracle.snapshot(lab, 'a')
            if extra_client:
                state = lab.state['nodes']['a']
                network = 'remote-node-a_default'
                lab.command('a', ['docker', 'run', '-d', '--name', 'lab-extra-pg-client', '--network', network,
                    '-e', 'PGPASSWORD=' + state['db_password'], lab.state['images']['postgres'],
                    'psql', '-h', 'postgres', '-U', 'gitea', '-d', 'gitea', '-c', 'SELECT pg_sleep(600)'])
                for _ in range(20):
                    count = oracle.sql(lab, 'a', "SELECT count(*) FROM pg_stat_activity WHERE query='SELECT pg_sleep(600)' AND pid<>pg_backend_pid()")
                    if int(count) > 0:
                        break
                    time.sleep(0.2)
                else:
                    raise CheckFailure('额外数据库客户端没有实际建立，拒绝把未命中注入算通过')
            else:
                lab.command('a', ['docker', 'stop', 'postgres'])
            try:
                task = lab.manager_api('POST', '/backups', json={'source_server_id': lab.state['nodes']['a']['server_id']}).json()
                result = lab.poll_task('backup', task['id'])
                if result['status'] != 'failed' or not result.get('error_msg'):
                    raise CheckFailure('数据库故障产生了可用假备份或没有明确错误')
            finally:
                if extra_client:
                    lab.command('a', ['docker', 'rm', '-f', 'lab-extra-pg-client'], check=False)
                else:
                    lab.command('a', ['docker', 'start', 'postgres'])
                lab.wait_gitea('a')
            changes = oracle.compare(before, oracle.snapshot(lab, 'a'))
            if changes:
                raise CheckFailure('数据库故障后源业务发生变化：' + json.dumps(changes, ensure_ascii=False))


def restore_interruption(lab, repeats=1):
    """实际停止导入中的目标 PG，验证半恢复不发布及原目标接管。"""
    from . import checkpoint
    from .import_watch import ImportObserver
    from .scenarios import write_json
    lab.login()
    with lab.case('database-import-preflight', '数据库导入中断整阶段空间与独立检查点'):
        # 两轮新备份及首轮两侧原现场都保留，不能为后续故障删除本轮冷恢复首败。
        lab.ensure_space(future_backups=repeats + 2)
        for endpoint in ('/backups', '/restore-tasks'):
            if any(row['status'] in ('running', 'pending') for row in lab.manager_api('GET', endpoint).json()):
                raise CheckFailure('已有活动备份恢复，尚未切换独立检查点或注入')
        if not all(checkpoint.available(lab, node) for node in ('a', 'b')):
            raise CheckFailure('缺少两侧已经验证的独立检查点，尚未注入')
    for repeat in range(1, repeats + 1):
        for node in ('a', 'b'):
            if repeat == 1:
                original_state = json.loads(json.dumps(lab.state['nodes'][node]))
                original_compose = lab.command(node, ['cat', '/lab/compose.json'])[1].decode()
                previous = set(lab.command(node, ['find', '/lab', '-mindepth', '1', '-maxdepth', '1',
                    '-type', 'd', '-name', 'previous-*'])[1].decode().splitlines())
                evidence = lab.root / ('database-import-initial-' + node + '-' + str(time.time_ns()) + '.json')
                retained = {'说明': '保留进入新故障前的全部原现场，包括冷恢复失败目标及其实际配置和身份；只在受限目录记录',
                            '节点': node, '节点状态': original_state, '原Compose': original_compose}
                write_json(evidence, retained)
                checkpoint.restore(lab, node, retain=True)
                current = set(lab.command(node, ['find', '/lab', '-mindepth', '1', '-maxdepth', '1',
                    '-type', 'd', '-name', 'previous-*'])[1].decode().splitlines())
                added = current - previous
                if len(added) != 1:
                    raise CheckFailure('原现场未唯一保留，停止后续故障')
                retained['保留持久卷目录'] = next(iter(added)); write_json(evidence, retained)
            else:
                checkpoint.restore(lab, node)
        lab.backup('a')
        before = oracle.snapshot(lab, 'b')
        with lab.case('database-partial', '目标真实数据库导入中断及原目标接管：第%d轮' % repeat):
            original = json.loads(lab.command('b', ['docker', 'inspect', 'postgres'])[1])[0]
            pg = original['Id']
            if not original['State']['Running'] or any(original['HostConfig'][key] for key in ('NanoCpus', 'CpuQuota', 'CpuPeriod')):
                raise CheckFailure('专属目标PG已有资源限制或未运行，不能覆盖未知配置')
            helpers = lab.command('b', ['docker', 'ps', '-aq', '--no-trunc', '--filter',
                'label=gitea.manager.operation'])[1].decode().splitlines()
            proof = {'说明': '不改SQL、样本、资源限制或产品；实际导入进程及唯一新助手匹配后才停止专属PG',
                     '轮次': repeat, '目标PG': pg}
            path = lab.root / ('database-import-' + str(time.time_ns()) + '.json')
            task = None
            try:
                with ImportObserver(lab, pg, helpers) as observer:
                    proof['提交前观察器就绪'] = observer.ready; write_json(path, proof)
                    task = lab.manager_api('POST', '/restore', json={'backup_id': lab.state['backup_id'],
                        'target_server_id': lab.state['nodes']['b']['server_id']}).json()
                    proof['恢复任务ID'] = task['id']; write_json(path, proof)
                    observer.arm(task['id'])
                    proof['真实导入中断'] = observer.wait(); write_json(path, proof)
                result = lab.poll_task('restore', task['id'], timeout=180)
                proof['中断后任务'] = result; write_json(path, proof)
                if result['status'] != 'failed' or not result.get('error_msg'):
                    raise CheckFailure('数据库导入故障没有明确失败')
                running = lab.command('b', ['docker', 'inspect', '-f', '{{.State.Running}}', 'gitea'])[1].strip()
                if running == b'true':
                    raise CheckFailure('数据库导入或回退失败后发布了半恢复实例')
            finally:
                lab.command('b', ['docker', 'start', pg])
            recovered = lab.manager_api('POST', '/restore-tasks/%s/recover' % task['id'], expected=None)
            proof['原目标接管HTTP状态'] = recovered.status_code; write_json(path, proof)
            if recovered.status_code != 200:
                raise CheckFailure('数据库恢复运行后无法接管原目标')
            lab.wait_gitea('b')
            after = oracle.snapshot(lab, 'b')
            changes = oracle.compare(before, after)
            proof.update({'之前快照': before['证据文件'], '之后快照': after['证据文件'], '差异': changes})
            write_json(path, proof)
            if changes:
                raise CheckFailure('导入失败接管后原目标仍有差异：' + json.dumps(changes, ensure_ascii=False))
