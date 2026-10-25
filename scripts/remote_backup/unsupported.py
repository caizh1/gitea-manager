"""以实际容器变更验证恢复准入；拒绝时目标数据必须保持。"""
import contextlib
import json

from .driver import CheckFailure
from .errors import EnvironmentBlocked
from . import oracle


@contextlib.contextmanager
def target_variant(lab, change):
    with lab.ssh('b') as client:
        with client.open_sftp() as sftp:
            with sftp.file('/lab/compose.json', 'rb') as stream:
                original = stream.read()
    config = lab.compose_config('b', original)
    change(config)
    try:
        lab.put('b', '/lab/compose.json', json.dumps(config))
        lab.command('b', ['docker', 'compose', '-f', '/lab/compose.json', 'up', '-d', '--force-recreate', 'gitea'])
        lab.wait_gitea('b')
        yield
    finally:
        lab.put('b', '/lab/compose.json', original)
        lab.command('b', ['docker', 'compose', '-f', '/lab/compose.json', 'up', '-d', '--force-recreate', 'gitea'])
        lab.wait_gitea('b')


def failures(lab):
    lab.login()
    before = oracle.snapshot(lab, 'b')
    name = 'remote-lab-incompatible:' + lab.state['run_id']
    lab.command('b', ['docker', 'commit', '--change', 'LABEL remote-lab.variant=incompatible', 'gitea', name])
    variants = [('不同镜像配置摘要', lambda config: config['services']['gitea'].update(image=name), '准入不匹配'),
                ('数据目录嵌套挂载', lambda config: config['services']['gitea']['volumes'].append('/lab/nested-test:/data/nested-test'), '嵌套挂载')]
    for label, change, expected in variants:
        with lab.case('unsupported-partial', label):
            with target_variant(lab, change):
                task = lab.manager_api('POST', '/restore', json={'backup_id': lab.state['backup_id'],
                    'target_server_id': lab.state['nodes']['b']['server_id']}).json()
                result = lab.poll_task('restore', task['id'])
                if result['status'] != 'failed' or expected not in result.get('error_msg', ''):
                    raise CheckFailure('准入没有在预期边界拒绝：' + result.get('error_msg', ''))
            after = oracle.snapshot(lab, 'b')
            if oracle.compare(before, after):
                raise CheckFailure('准入拒绝后原目标发生数据变化')


def pg_major(lab):
    """真实 PG17 空白目标，不能用改版本字符串或不相干配置拒绝替代主版本判断。"""
    from .checkpoint import empty_target
    lab.login()
    with lab.case('unsupported-partial', '真实 PostgreSQL 17 目标拒绝 PostgreSQL 16 备份'):
        image = lab.state.get('pg_variant_image')
        if not image:
            raise EnvironmentBlocked('尚未加载经过身份核对的本机 PG17 镜像，需先执行 load-pg17')
        source_major = int(oracle.sql(lab, 'a', 'SHOW server_version_num')) // 10000
        if source_major != 16 or lab.state.get('backup_node') != 'a':
            raise CheckFailure('需要当前源 PG16 的有效真实备份，不能伪造主版本信息')
        with empty_target(lab, postgres_image=image):
            target_major = int(oracle.sql(lab, 'b', 'SHOW server_version_num')) // 10000
            if target_major != 17:
                raise CheckFailure('实际目标 PG 主版本不是 17，未命中准入样本')
            # 新安装在首个仓库创建前没有仓库根目录；建立真实目标哨兵，保持核验器严格检查。
            from .fixtures import ensure_repo
            ensure_repo(lab, 'b', 'pg17-target-only', auto_init=True)
            before = oracle.snapshot(lab, 'b')
            task = lab.manager_api('POST', '/restore', json={'backup_id': lab.state['backup_id'],
                'target_server_id': lab.state['nodes']['b']['server_id']}).json()
            result = lab.poll_task('restore', task['id'])
            after = oracle.snapshot(lab, 'b')
            differences = oracle.compare(before, after)
            path = lab.root / ('postgres-major-rejection-' + str(task['id']) + '.json')
            path.write_text(json.dumps({'说明': '真实不同 PG 主版本的有效空白目标，拒绝后独立全表对照',
                '源主版本': source_major, '目标主版本': target_major, '任务ID': task['id'],
                '任务状态': result['status'], '错误': lab.scrub(result.get('error_msg', '')), '差异': differences}, ensure_ascii=False, indent=2) + '\n')
            path.chmod(0o600)
            if result['status'] != 'failed' or '数据库主版本' not in result.get('error_msg', '') or differences:
                raise CheckFailure('主版本没有明确拒绝或拒绝后目标数据变化，详见 ' + path.name)
