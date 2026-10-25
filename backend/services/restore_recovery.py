"""恢复检查点落在目标持久卷；同一 Manager 使用文件锁排除活跃工作进程。"""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile

from services.backup_consistency import INSTANCE_LOCK, execute, require_quiet
from services.restore_secrets import read_file, put_file, SecretTransition

STATE = INSTANCE_LOCK + '/state.json'
PHASES = ('准备', '停止', '数据库修改', '文件切换', '启动配置', '发布', '完成')


def lease(target):
    from config import BACKUP_DIR
    root = Path(BACKUP_DIR).parent / 'restore-locks'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    from services.docker_service import managed_client
    client = managed_client(target)
    try:
        database = client.containers.get(target.pg_container)
        key = hashlib.sha256((database.id + ':' + target.pg_dbname).encode()).hexdigest()
    finally:
        client.close()
    stream = (root / key).open('a')
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError('该实例仍有活跃恢复进程，拒绝接管') from None
    return stream


def copy_archive(source, path, destination, directory):
    with tempfile.TemporaryFile() as stream:
        for block in source.get_archive(path)[0]:
            stream.write(block)
        stream.seek(0)
        if not destination.put_archive(directory, stream):
            raise RuntimeError('持久化数据库回退点失败')


def restore_ssh(helper, stage):
    # 保留快照以便恢复进程再次中断后重放；授权还原完成前不得开放 SSH。
    execute(helper, ['sh', '-ec', f'if test -d {stage}/original-ssh; then rm -rf /data/git/.ssh; cp -a {stage}/original-ssh /data/git/.ssh; elif test -f {stage}/original-ssh-absent; then rm -rf /data/git/.ssh; else exit 1; fi; sync'])


class Journal:
    def __init__(self, helper, database, target, task, transition, stage, pg_temp, new_token):
        self.helper = helper
        self.value = {'助手': helper.id, '版本': 1, '阶段': '准备', '原容器': transition.original.id,
            '数据库容器': database.id, '容器名': transition.name, '标识': transition.marker,
            '暂存': stage, '数据库暂存': pg_temp, '原配置': transition.attrs,
            '原令牌': target.api_token, '新令牌': new_token, '任务': task.id,
            '数据库用户': target.pg_user, '数据库名称': target.pg_dbname}
        self.save('准备')

    def prepared(self, transition):
        if transition.compose_helper:
            self.value['Compose原文'] = base64.b64encode(transition.compose_original).decode()
            self.value['Compose新文'] = base64.b64encode(transition.compose_new).decode()
        self.save('准备')

    def save(self, phase):
        self.value['阶段'] = phase
        put_file(self.helper, STATE + '.tmp', json.dumps(self.value, ensure_ascii=False).encode(), 0o600, 0, 0)
        execute(self.helper, ['sh', '-ec', 'mv "$1" "$2"; sync', 'sh', STATE + '.tmp', STATE])


def recover(target):
    """发布之前只回退；跨过发布边界后只启动和检查当前数据，绝不覆盖新写入。"""
    from docker.errors import NotFound
    from docker.types import Mount
    from services.docker_service import managed_client
    from services.commit_service import _require_target_health
    from services.restore_isolation import retire_network
    from models import db, RestoreTask
    lock = lease(target)
    client = managed_client(target)
    helper = transition = None
    try:
        try:
            current = client.containers.get(target.gitea_container)
        except NotFound:
            matches = [c for c in client.containers.list(all=True)
                       if c.name.startswith(target.gitea_container + '-manager-restore-')]
            if len(matches) != 1:
                raise RuntimeError('无法唯一定位中断前的目标容器') from None
            current = matches[0]
        helper = client.containers.run(current.attrs['Image'], entrypoint=['sleep'], command=['infinity'],
            volumes_from=[current.id], network_mode='none', detach=True,
            labels={'gitea.manager.recovery': current.id})
        state = json.loads(read_file(helper, STATE)[0])
        marker = state['标识']
        if (state.get('版本') != 1 or state.get('阶段') not in PHASES
                or not marker.startswith('manager-restore-') or len(marker) != 48
                or any(ch not in '0123456789abcdef' for ch in marker[16:])
                or state['暂存'] != '/data/' + marker or state['数据库暂存'] != '/tmp/' + marker
                or state['容器名'] != target.gitea_container
                or state['数据库用户'] != target.pg_user or state['数据库名称'] != target.pg_dbname):
            raise RuntimeError('中断恢复清单与目标不一致，保留现场')
        database = client.containers.get(target.pg_container)
        if database.id != state['数据库容器']:
            raise RuntimeError('数据库容器已被替换，拒绝自动接管')
        # 只清理本事务的验证容器，不触碰其他业务容器。
        for container in client.containers.list(all=True, filters={'label': 'gitea.manager.operation=' + marker}):
            if container.name == marker + '-verify':
                container.remove(force=True)
        pending_networks = []
        for network in client.networks.list(filters={'label': 'gitea.manager.operation=' + marker}):
            if not retire_network(network, database):
                pending_networks.append(network)
        if current.id != state['原容器'] and (current.attrs['Config'].get('Labels') or {}).get('gitea.manager.restore') != marker:
            raise RuntimeError('目标容器被外部替换，拒绝接管')
        published = PHASES.index(state['阶段']) >= PHASES.index('发布')
        if not published:
            original = client.containers.get(state['原容器'])
            if current.id != original.id and (current.attrs['Config'].get('Labels') or {}).get('gitea.manager.restore') != marker:
                raise RuntimeError('目标容器被外部替换，拒绝覆盖')
            current.stop(timeout=60)
            original.stop(timeout=60)
            require_quiet(database, target)
            stage, pg_temp = state['暂存'], state['数据库暂存']
            if PHASES.index(state['阶段']) >= PHASES.index('文件切换'):
                execute(helper, ['sh', '-ec', f'if test -d {stage}/old-gitea; then rm -rf /data/gitea; mv {stage}/old-gitea /data/gitea; fi; if test -d {stage}/old-repositories; then rm -rf /data/git/repositories; mv {stage}/old-repositories /data/git/repositories; fi; if test -d {stage}/old-lfs; then rm -rf /data/git/lfs; mv {stage}/old-lfs /data/git/lfs; fi'])
                if state.get('Actions目录'):
                    from services.backup_actions import rollback_command
                    execute(helper, ['sh', '-ec', rollback_command(stage, state['Actions目录'])])
            if PHASES.index(state['阶段']) >= PHASES.index('数据库修改'):
                execute(database, ['mkdir', '-p', pg_temp], user='postgres')
                copy_archive(helper, stage + '/original.sql', database, pg_temp)
                execute(database, ['dropdb', '-U', target.pg_user, '--if-exists', '--force', target.pg_dbname], user='postgres')
                execute(database, ['createdb', '-U', target.pg_user, '-O', target.pg_user, target.pg_dbname], user='postgres')
                execute(database, ['psql', '-X', '-v', 'ON_ERROR_STOP=1', '--single-transaction', '-U', target.pg_user, '-d', target.pg_dbname, '-f', pg_temp + '/original.sql'], user='postgres')
            transition = SecretTransition(client, original, marker)
            transition.attrs = state['原配置']
            transition.name = state['容器名']
            transition.current = current
            original.reload()
            transition.renamed = original.name != transition.name
            transition.disconnected = list(set(transition.attrs['NetworkSettings']['Networks']) - set(original.attrs['NetworkSettings']['Networks']))
            if 'Compose原文' in state:
                labels = transition.attrs['Config']['Labels']
                path = PurePosixPath(labels['com.docker.compose.project.config_files'].split(',')[-1])
                transition.compose_name = path.name
                transition.compose_helper = client.containers.run(transition.attrs['Image'], entrypoint=['sleep'],
                    command=['infinity'], network_mode='none', detach=True,
                    mounts=[Mount('/compose', str(path.parent), type='bind')])
                _, transition.compose_stat = read_file(transition.compose_helper, '/compose/' + path.name)
                transition.compose_original = base64.b64decode(state['Compose原文'])
                transition.compose_new = base64.b64decode(state['Compose新文'])
                transition.compose_changed = True
            current = transition.rollback()
            if PHASES.index(state['阶段']) >= PHASES.index('数据库修改'):
                if state.get('SSH快照'):
                    restore_ssh(helper, stage)
                else:
                    # 兼容升级前已经中断的事务，没有快照时从原数据库重新生成。
                    current.reload(); database.reload()
                    common = set(current.attrs['NetworkSettings']['Networks']) & set(database.attrs['NetworkSettings']['Networks'])
                    if not common:
                        raise RuntimeError('无法连接原数据库重建 SSH 授权，保持停机')
                    client.containers.run(current.attrs['Image'], entrypoint=['/usr/local/bin/gitea'],
                        command=['--config', '/data/gitea/conf/app.ini', 'admin', 'regenerate', 'keys'],
                        user='git', volumes_from=[current.id], network=sorted(common)[0], remove=True,
                        labels={'gitea.manager.recovery': current.id})
            target.api_token = state['原令牌']
        else:
            target.api_token = state['新令牌']
        state['新令牌'] = target.api_token
        if not published:
            state['已回退'] = True
        state['阶段'] = '发布'
        put_file(helper, STATE + '.tmp', json.dumps(state, ensure_ascii=False).encode(), 0o600, 0, 0)
        execute(helper, ['sh', '-ec', 'mv "$1" "$2"; sync', 'sh', STATE + '.tmp', STATE])
        current.start()
        # 离线新建空实例没有原令牌；只有回退该状态才允许仅检查启动。
        if state.get('已回退') and not state['原令牌']:
            _require_target_health(target, require_auth=False)
        else:
            _require_target_health(target)
        for network in pending_networks:
            retire_network(network, database)
        task = db.session.get(RestoreTask, state['任务'])
        if task and task.target_server_id == target.id:
            task.status = 'failed' if state.get('已回退') else 'success'
            task.error_msg = '中断恢复已回退原数据，请重新发起恢复' if state.get('已回退') else ''
        db.session.commit()
        execute(helper, ['rm', '-rf', state['暂存']])
        execute(database, ['rm', '-rf', state['数据库暂存']], user='postgres')
        execute(helper, ['rm', '-rf', INSTANCE_LOCK])
        try:
            abandoned = client.containers.get(state['助手'])
            if abandoned.id != helper.id:
                abandoned.remove(force=True)
        except NotFound:
            pass
        return '当前数据启动验证通过，未覆盖发布后的写入' if published else '原数据及启动配置已回退并验证'
    finally:
        try:
            if transition is not None:
                transition.close()
            if helper is not None:
                helper.remove(force=True)
        finally:
            client.close()
            lock.close()
