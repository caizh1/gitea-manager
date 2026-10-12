"""恢复先准备完整文件与数据库回退点，任何检查失败均停止发布成功状态。"""
import io
import configparser
import os
from pathlib import Path
import shutil
import shlex
import stat
import tarfile
import tempfile
import uuid
import zipfile

from services.diagnostics import CURRENT
from services.operation_progress import ProgressReader, finish_current_step


def restore(target, backup, task, progress):
    from services.docker_service import managed_client
    client = managed_client(target)
    try:
        return _restore(client, target, backup, task, progress)
    except Exception:
        client.close()
        raise


def _restore(client, target, backup, task, progress):
    gitea = client.containers.get(target.gitea_container)
    database = client.containers.get(target.pg_container)
    marker = 'manager-restore-' + uuid.uuid4().hex
    stage = '/data/' + marker
    pg_temp = '/tmp/' + marker
    swapped = False
    stopped = False
    db_changed = False
    success = False
    helper = None
    report = CURRENT.get()
    finalized = False
    locked = False
    rollback_complete = False

    def execute(container, args, label, user=None):
        progress(task, task.progress_stage or 'prepare', task.progress_label or label, task.progress_percent or 0, label)
        if report: report.event('执行', label, 容器=container.id[:12])
        if target.connection_mode == 'local':
            code, raw = container.exec_run(args, user=user)
            output = raw.decode('utf-8', errors='replace')
        else:
            from services.ssh_service import SSHService
            command = ['docker', 'exec'] + (['--user', user] if user else []) + [container.id, *args]
            code, out, err = SSHService(target.host, target.ssh_port, target.ssh_user).exec(shlex.join(command), timeout=600)
            output = out + ('\n' + err if err else '')
        if report: report.event('完成' if code == 0 else '失败', label, 退出码=code, 输出=output[-12000:])
        if code != 0:
            raise RuntimeError(f'{label}：退出码 {code}；{output[-3000:]}')
        return output

    def sql(args, label):
        return execute(database, args, label, user='postgres')

    def reset_database(path, label):
        sql(['dropdb', '-U', target.pg_user, '--if-exists', '--force', target.pg_dbname], label+'：删除数据库')
        sql(['createdb', '-U', target.pg_user, '-O', target.pg_user, target.pg_dbname], label+'：创建数据库')
        sql(['psql', '-X', '-v', 'ON_ERROR_STOP=1', '--single-transaction', '-U', target.pg_user,
             '-d', target.pg_dbname, '-f', path], label+'：导入数据库')

    with tempfile.TemporaryDirectory(prefix='manager-restore-') as workspace:
        workspace = Path(workspace)
        progress(task, 'validate_archive', '正在校验备份文件和空间', 10, backup.filename)
        with zipfile.ZipFile(backup.file_path) as archive:
            members = archive.infolist()
            total = sum(x.file_size for x in members)
            if shutil.disk_usage(workspace).free < total * 2 + 64*1024*1024:
                raise RuntimeError('Manager 临时空间不足，尚未修改目标数据')
            for entry in members:
                path = Path(entry.filename)
                if path.is_absolute() or '..' in path.parts or stat.S_ISLNK(entry.external_attr >> 16):
                    raise RuntimeError('备份包含不安全路径或符号链接，拒绝恢复')
            if archive.testzip() is not None:
                raise RuntimeError('备份 ZIP 校验失败，尚未修改目标数据')
            progress(task, 'extract_archive', '正在解压备份文件', 15, backup.filename)
            for index, entry in enumerate(members):
                progress(task, 'extract_archive', '正在解压备份文件', 15, backup.filename,
                         current=index, total=len(members), unit='files', item=entry.filename)
                archive.extract(entry, workspace)
            progress(task, 'extract_archive', '正在解压备份文件', 15, backup.filename,
                     current=len(members), total=len(members), unit='files')
        roots = list(workspace.rglob('gitea-db.sql'))
        if len(roots) != 1:
            raise RuntimeError('备份必须包含唯一数据库 SQL，尚未修改目标数据')
        root = roots[0].parent
        if not (root/'data').is_dir() or not (root/'repos').is_dir():
            raise RuntimeError('备份缺少数据或仓库目录，尚未修改目标数据')
        environment = dict(item.split('=', 1) for item in gitea.attrs['Config'].get('Env', []) if '=' in item)
        secret_uri = 'file:/data/gitea/conf/manager-secret-key'
        if environment.get('GITEA__security__SECRET_KEY_URI') != secret_uri or environment.get('GITEA__security__SECRET_KEY'):
            raise RuntimeError('目标未使用支持恢复的持久加密密钥配置，拒绝修改；请使用新版一键包建立恢复目标')
        source_config = configparser.ConfigParser(interpolation=None)
        try:
            source_config.read_string('[DEFAULT]\n'+(root/'app.ini').read_text())
            uri = source_config.get('security', 'SECRET_KEY_URI', fallback='')
            if uri and uri != secret_uri:
                raise RuntimeError('备份使用外置加密密钥，无法确认密钥完整性，拒绝修改目标')
            source_secret = ((root/'data/conf/manager-secret-key').read_text().strip() if uri else source_config.get('security', 'SECRET_KEY'))
        except (OSError, configparser.Error) as exc:
            raise RuntimeError('备份缺少可验证的数据加密密钥，拒绝修改目标') from None
        if not source_secret: raise RuntimeError('备份的数据加密密钥为空，拒绝修改目标')
        from services.diagnostics import register_secret
        register_secret(source_secret)
        secret_file = root/'data/conf/manager-secret-key'
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(source_secret); secret_file.chmod(0o600)
        common_networks = set(gitea.attrs['NetworkSettings']['Networks']) & set(database.attrs['NetworkSettings']['Networks'])
        if not common_networks: raise RuntimeError('Gitea 与 PostgreSQL 没有共同网络，尚未修改目标数据')
        helper = client.containers.run(gitea.attrs['Config']['Image'], entrypoint=['sleep'], command=['infinity'],
                                      volumes_from=[gitea.id], network=sorted(common_networks)[0], detach=True,
                                      labels={'gitea.manager.operation': report.id if report else marker})
        try:
            execute(helper, ['mkdir', '/data/.manager-restore-lock'], '获取目标实例恢复锁；已有恢复或未完成回退时拒绝并发操作')
            locked = True
            free = int(execute(helper, ['sh', '-ec', "df -Pk /data | tail -1 | awk '{print $4}'"], '检查目标磁盘空间').strip())*1024
            if free < total * 2 + 64*1024*1024:
                raise RuntimeError('目标数据盘空间不足，尚未修改目标数据')
            execute(helper, ['mkdir', '-p', stage], '建立文件暂存区')
            progress(task, 'prepare_files', '正在准备仓库和数据文件', 20, target.name)
            with tempfile.TemporaryFile() as stream:
                with tarfile.open(fileobj=stream, mode='w') as bundle:
                    bundle.add(root/'data', arcname='new-gitea')
                    bundle.add(root/'repos', arcname='new-repositories')
                stream.seek(0)
                progress(task, 'transfer_files', '正在传输仓库和数据文件', 25, target.name)
                reader = ProgressReader(stream, lambda n, total: progress(task, 'transfer_files', '正在传输仓库和数据文件', 25, target.name, current=n, total=total, unit='bytes'))
                if not helper.put_archive(stage, reader): raise RuntimeError('写入文件暂存区失败')
            execute(helper, ['sh','-ec', f'mkdir -p {stage}/new-gitea/conf; cp -p /data/gitea/conf/app.ini {stage}/new-gitea/conf/app.ini; chown -R git:git {stage}/new-gitea {stage}/new-repositories'], '保留目标配置并校验文件权限')
            sql(['mkdir', '-m', '700', pg_temp], '建立数据库暂存区')
            with tempfile.TemporaryFile() as stream:
                with tarfile.open(fileobj=stream, mode='w') as bundle:
                    info = bundle.gettarinfo(str(roots[0]), arcname='incoming.sql')
                    info.mode = 0o644
                    with roots[0].open('rb') as source: bundle.addfile(info, source)
                stream.seek(0)
                progress(task, 'transfer_database', '正在传输数据库文件', 30, target.pg_dbname)
                reader = ProgressReader(stream, lambda n, total: progress(task, 'transfer_database', '正在传输数据库文件', 30, target.pg_dbname, current=n, total=total, unit='bytes', item='incoming.sql'))
                if not database.put_archive(pg_temp, reader): raise RuntimeError('写入数据库暂存区失败')
            progress(task, 'stop_gitea', '停止目标服务并保存回退点', 35, target.name)
            gitea.stop(timeout=30); stopped = True
            progress(task, 'save_rollback', '正在保存原数据库回退点', 40, target.pg_dbname)
            sql(['pg_dump', '-U', target.pg_user, '-d', target.pg_dbname, '-f', pg_temp+'/original.sql'], '保存原数据库')
            progress(task, 'import_database', '正在导入并校验数据库', 55, target.pg_dbname)
            db_changed = True
            reset_database(pg_temp+'/incoming.sql', '恢复备份')
            progress(task, 'copy_files', '正在切换已准备的文件', 75, target.name)
            # 每个重命名均检查；失败时根据原目录存在状态恢复，避免半次切换。
            swapped = True
            execute(helper, ['sh','-ec', f'mkdir -p /data/git/repositories; chown git:git /data/git/repositories; mv /data/gitea {stage}/old-gitea; mv /data/git/repositories {stage}/old-repositories; mv {stage}/new-gitea /data/gitea; mv {stage}/new-repositories /data/git/repositories'], '切换仓库和数据目录')
            progress(task, 'regenerate_hooks', '正在重新生成仓库 Hook', 80, target.name)
            execute(helper, ['/usr/local/bin/gitea','--config','/data/gitea/conf/app.ini','admin','regenerate','hooks'], '重新生成仓库 Hook', user='git')
            progress(task, 'regenerate_keys', '正在重新生成 Git SSH 公钥', 85, target.name)
            execute(helper, ['/usr/local/bin/gitea','--config','/data/gitea/conf/app.ini','admin','regenerate','keys'], '重新生成 Git SSH 公钥', user='git')
            progress(task, 'start_gitea', '正在启动目标服务', 90, target.name)
            gitea.start(); stopped = False; success = True
            def finalize(verified):
                nonlocal finalized
                if finalized: return
                finalized = True
                try:
                    if not verified:
                        progress(task, 'rollback', '验证失败，正在回退原数据', 98, target.name)
                        gitea.stop(timeout=30)
                        execute(helper,['sh','-ec',f'rm -rf /data/gitea /data/git/repositories; mv {stage}/old-gitea /data/gitea; mv {stage}/old-repositories /data/git/repositories'],'验证失败，回退原文件')
                        reset_database(pg_temp+'/original.sql','验证失败，回退原数据库')
                        execute(helper, ['/usr/local/bin/gitea','--config','/data/gitea/conf/app.ini','admin','regenerate','keys'], '恢复原 Git SSH 公钥授权', user='git')
                        gitea.start()
                    progress(task, 'cleanup', '正在清理暂存文件与释放恢复锁', 99, target.name)
                    execute(helper,['rm','-rf',stage],'清理文件暂存区')
                    sql(['rm','-rf',pg_temp],'清理数据库暂存区')
                    execute(helper, ['rmdir', '/data/.manager-restore-lock'], '释放实例恢复锁')
                    finish_current_step(task, '暂存文件已清理，恢复锁已释放')
                finally:
                    try: helper.remove(force=True)
                    finally: client.close()
            return finalize
        except Exception as primary:
            if report: report.fail(primary)
            if not getattr(primary, 'operation_stage', None): primary.operation_stage = task.progress_stage
            try:
                progress(task, 'rollback', '恢复失败，正在回退原数据', task.progress_percent or 0, str(primary))
                if swapped:
                    execute(helper, ['sh','-ec', f'if test -d {stage}/old-gitea; then rm -rf /data/gitea; mv {stage}/old-gitea /data/gitea; fi; if test -d {stage}/old-repositories; then rm -rf /data/git/repositories; mv {stage}/old-repositories /data/git/repositories; fi'], '回退原文件')
                if db_changed:
                    reset_database(pg_temp+'/original.sql', '回退原数据库')
                    execute(helper, ['/usr/local/bin/gitea','--config','/data/gitea/conf/app.ini','admin','regenerate','keys'], '恢复原 Git SSH 公钥授权', user='git')
                if stopped: gitea.start(); stopped = False
                finish_current_step(task, '原数据已回退，目标服务已启动')
                rollback_complete = True
            except Exception as rollback:
                raise RuntimeError(f'恢复失败：{primary}；自动回退未完成：{rollback}；回退点保留于 {stage} 和 {pg_temp}，目标服务保持停止') from primary
            raise
        finally:
            if helper is not None and not success:
                try:
                    if locked and rollback_complete:
                        execute(helper, ['rmdir', '/data/.manager-restore-lock'], '释放实例恢复锁')
                finally: helper.remove(force=True)
