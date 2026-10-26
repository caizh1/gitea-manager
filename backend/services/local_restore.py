"""恢复先准备完整文件与数据库回退点，任何检查失败均停止发布成功状态。"""
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import tarfile
import tempfile
import uuid
import zipfile

from services.diagnostics import CURRENT
from services.operation_progress import ProgressReader, finish_current_step
from services.restore_secrets import SecretTransition, archive_secret, SECRET_URI, read_file, security_values, uri_path
from services.backup_consistency import check_layout, require_quiet, load_snapshot, repository_snapshot, verify_snapshot
from services.restore_archive import validate_members, restore_modes, custom_hooks, verify_hooks
from services.restore_isolation import IsolatedGitea


def restore(target, backup, task, progress):
    from services.backup_recovery import ensure_ready
    ensure_ready(target.id)
    from services.docker_service import managed_client
    from services.restore_recovery import lease
    lock = lease(target)
    client = None
    try:
        client = managed_client(target)
        finalizer = _restore(client, target, backup, task, progress)
        def finish(verified):
            try:
                return finalizer(verified)
            finally:
                lock.close()
        finish.api_get_json = finalizer.api_get_json
        finish.snapshot_check = finalizer.snapshot_check
        return finish
    except BaseException:
        if client is not None:
            client.close()
        lock.close()
        raise


def _restore(client, target, backup, task, progress):
    gitea = client.containers.get(target.gitea_container)
    database = client.containers.get(target.pg_container)
    check_layout(gitea, database, target, restoring=True)
    from services.backup_manifest import load_manifest, identity, admit, table_snapshot
    manifest = load_manifest(backup.file_path)
    requirements = identity(gitea, database, target)
    admit(manifest, requirements)
    from services.backup_actions import external_path, ARCHIVE_ROOT, switch_command, rollback_command, verify_artifacts
    target_config = read_file(gitea, '/data/gitea/conf/app.ini')[0]
    artifacts = external_path(target_config)
    external_lfs = requirements['存储'].get('lfs', {}).get('path') == '/data/git/lfs'
    if manifest is None:
        from services.backup_consistency import database_sql
        if database_sql(database, target, "SELECT to_regclass('public.governance_namespace') IS NOT NULL") == 't':
            raise RuntimeError('增强版恢复必须提供灾备准入清单，请重新生成备份；尚未修改目标数据')
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
    isolated = None
    journal = None
    transition = SecretTransition(client, gitea, marker)

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
            validate_members(members)
            if archive.testzip() is not None:
                raise RuntimeError('备份 ZIP 校验失败，尚未修改目标数据')
            actions_snapshot = json.loads(archive.read('manager-actions.json')) if 'manager-actions.json' in archive.namelist() else None
            progress(task, 'extract_archive', '正在解压备份文件', 15, backup.filename)
            for index, entry in enumerate(members):
                progress(task, 'extract_archive', '正在解压备份文件', 15, backup.filename,
                         current=index, total=len(members), unit='files', item=entry.filename)
                archive.extract(entry, workspace)
            restore_modes(workspace, members)
            progress(task, 'extract_archive', '正在解压备份文件', 15, backup.filename,
                     current=len(members), total=len(members), unit='files')
        roots = list(workspace.rglob('gitea-db.sql'))
        if len(roots) != 1:
            raise RuntimeError('备份必须包含唯一数据库 SQL，尚未修改目标数据')
        root = roots[0].parent
        if manifest is None:
            import re
            with roots[0].open(errors='replace') as stream:
                if any(re.match(r'^CREATE TABLE(?: IF NOT EXISTS)? [\"`]?governance_', line, re.I) for line in stream):
                    raise RuntimeError('增强版历史备份缺少构建准入清单，拒绝未经匹配的恢复')
        if not (root/'data').is_dir() or not (root/'repos').is_dir():
            raise RuntimeError('备份缺少数据或仓库目录，尚未修改目标数据')
        if artifacts and not (workspace / ARCHIVE_ROOT).is_dir():
            raise RuntimeError('备份缺少独立 Actions 制品正文，尚未修改目标数据')
        hooks = custom_hooks(root/'repos')
        snapshot = load_snapshot(backup.file_path)
        progress(task, 'prepare_secret', '正在校验备份密钥与目标启动配置', 18, target.name)
        source_secret = archive_secret(root)
        from services.diagnostics import register_secret
        register_secret(source_secret)
        secret_file = root/'data/conf/manager-secret-key'
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(source_secret); secret_file.chmod(0o600)
        (secret_file.parent/'manager-secret-uri').write_text(SECRET_URI)
        (secret_file.parent/'manager-empty-secret').write_text('')
        from services.restore_config import RestoreConfig
        business_config = RestoreConfig(root, workspace, target_config, SECRET_URI)
        transition.configure_business(business_config)
        common_networks = set(gitea.attrs['NetworkSettings']['Networks']) & set(database.attrs['NetworkSettings']['Networks'])
        if not common_networks: raise RuntimeError('Gitea 与 PostgreSQL 没有共同网络，尚未修改目标数据')
        helper = client.containers.run(gitea.attrs['Image'], entrypoint=['sleep'], command=['infinity'],
                                      volumes_from=[gitea.id], network=sorted(common_networks)[0], detach=True,
                                      labels={'gitea.manager.operation': marker})
        try:
            execute(helper, ['mkdir', '/data/.manager-restore-lock'], '获取目标实例恢复锁；已有恢复或未完成回退时拒绝并发操作')
            locked = True
            from services.restore_recovery import Journal, copy_archive, restore_ssh
            journal = Journal(helper, database, target, task, transition, stage, pg_temp, backup.source_api_token)
            journal.value['Actions目录'] = artifacts
            transition.prepare()
            business_config.write()
            journal.prepared(transition)
            free = int(execute(helper, ['sh', '-ec', "df -Pk /data | tail -1 | awk '{print $4}'"], '检查目标磁盘空间').strip())*1024
            if free < total * 2 + 64*1024*1024:
                raise RuntimeError('目标数据盘空间不足，尚未修改目标数据')
            execute(helper, ['mkdir', '-p', stage], '建立文件暂存区')
            progress(task, 'prepare_files', '正在准备仓库和数据文件', 20, target.name)
            with tempfile.TemporaryFile() as stream:
                with tarfile.open(fileobj=stream, mode='w') as bundle:
                    bundle.add(root/'data', arcname='new-gitea')
                    bundle.add(root/'repos', arcname='new-repositories')
                    if artifacts:
                        bundle.add(workspace / ARCHIVE_ROOT, arcname='new-actions')
                stream.seek(0)
                progress(task, 'transfer_files', '正在传输仓库和数据文件', 25, target.name)
                reader = ProgressReader(stream, lambda n, total: progress(task, 'transfer_files', '正在传输仓库和数据文件', 25, target.name, current=n, total=total, unit='bytes'))
                if not helper.put_archive(stage, reader): raise RuntimeError('写入文件暂存区失败')
            execute(helper, ['sh','-ec', f'chown -R git:git {stage}/new-gitea {stage}/new-repositories'], '校验源业务配置及文件权限，保留明确目标部署参数')
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
            journal.save('停止')
            gitea.stop(timeout=30); stopped = True
            require_quiet(database, target)
            progress(task, 'save_rollback', '正在保存原数据库回退点', 40, target.pg_dbname)
            sql(['pg_dump', '-U', target.pg_user, '-d', target.pg_dbname, '-f', pg_temp+'/original.sql'], '保存原数据库')
            progress(task, 'import_database', '正在导入并校验数据库', 55, target.pg_dbname)
            copy_archive(database, pg_temp+'/original.sql', helper, stage)
            execute(helper, ['sh', '-ec', f'if test -d /data/git/.ssh; then cp -a /data/git/.ssh {stage}/original-ssh; else touch {stage}/original-ssh-absent; fi; sync'], '保存原 SSH 授权回退点')
            journal.value['SSH快照'] = True
            journal.save('数据库修改')
            db_changed = True
            reset_database(pg_temp+'/incoming.sql', '恢复备份')
            if manifest is not None and table_snapshot(database, target) != manifest['数据表']:
                raise RuntimeError('恢复后的业务数据表与停写快照不一致，拒绝启动并回退')
            progress(task, 'copy_files', '正在切换已准备的文件', 75, target.name)
            # 每个重命名均检查；失败时根据原目录存在状态恢复，避免半次切换。
            journal.save('文件切换')
            swapped = True
            execute(helper, ['sh','-ec', f'mkdir -p /data/git/repositories; chown git:git /data/git/repositories; mv /data/gitea {stage}/old-gitea; mv /data/git/repositories {stage}/old-repositories; mv {stage}/new-gitea /data/gitea; mv {stage}/new-repositories /data/git/repositories'], '切换仓库和数据目录')
            if artifacts:
                execute(helper, ['sh', '-ec', switch_command(stage, artifacts)], '切换 Actions 制品正文')
            if external_lfs:
                execute(helper, ['sh', '-ec', f'mkdir -p /data/git/lfs; mv /data/git/lfs {stage}/old-lfs; mkdir -p /data/gitea/lfs; mv /data/gitea/lfs /data/git/lfs; chown -R git:git /data/git/lfs'], '恢复持久 LFS 对象')
            if actions_snapshot:
                verify_artifacts(helper, actions_snapshot)
            progress(task, 'configure_secret', '正在同步目标密钥启动配置', 78, target.name)
            journal.save('启动配置')
            gitea = transition.switch()
            transition.verify_environment()
            business_config.verify(read_file(gitea, '/data/gitea/conf/app.ini')[0])
            progress(task, 'regenerate_hooks', '正在重新生成仓库 Hook', 80, target.name)
            execute(helper, ['/usr/local/bin/gitea','--config','/data/gitea/conf/app.ini','admin','regenerate','hooks'], '重新生成仓库 Hook', user='git')
            verify_hooks(helper, hooks)
            progress(task, 'regenerate_keys', '正在重新生成 Git SSH 公钥', 85, target.name)
            execute(helper, ['/usr/local/bin/gitea','--config','/data/gitea/conf/app.ini','admin','regenerate','keys'], '重新生成 Git SSH 公钥', user='git')
            progress(task, 'start_gitea', '正在隔离网络中验证恢复实例，业务端口保持关闭', 90, target.name)
            isolated = IsolatedGitea(client, gitea, database, target, marker,
                                    validation_token=(backup.source_api_token or '').strip() or target.api_token)
            isolated.start()
            progress(task, 'verify_secret', '正在回读目标生效的加密密钥配置', 92, target.name)
            actual_secret, actual_uri = security_values(read_file(isolated.container, '/tmp/manager-verify.ini')[0])
            if actual_uri:
                actual_secret = read_file(gitea, uri_path(actual_uri))[0].decode().strip()
            if actual_secret != source_secret:
                raise RuntimeError('目标启动后密钥与备份不一致，拒绝发布成功并回退')
            success = True
            def snapshot_check(saved_rows):
                isolated.close()
                require_quiet(database, target)
                verify_hooks(helper, hooks)
                if actions_snapshot:
                    verify_artifacts(helper, actions_snapshot)
                verify_snapshot(snapshot, repository_snapshot(helper, database, target), saved_rows)

            def finalize(verified):
                nonlocal finalized, gitea
                if finalized: return
                finalized = True
                try:
                    isolated.close()
                    if not verified:
                        progress(task, 'rollback', '验证失败，正在回退原数据', 98, target.name)
                        gitea.stop(timeout=30)
                        execute(helper,['sh','-ec',f'rm -rf /data/gitea /data/git/repositories; mv {stage}/old-gitea /data/gitea; mv {stage}/old-repositories /data/git/repositories; if test -d {stage}/old-lfs; then rm -rf /data/git/lfs; mv {stage}/old-lfs /data/git/lfs; fi'],'验证失败，回退原文件')
                        if artifacts:
                            execute(helper, ['sh', '-ec', rollback_command(stage, artifacts)], '回退 Actions 制品正文')
                        reset_database(pg_temp+'/original.sql','验证失败，回退原数据库')
                        gitea = transition.rollback()
                        restore_ssh(helper, stage)
                        journal.value['新令牌'] = journal.value['原令牌']
                        journal.value['已回退'] = True
                        journal.save('发布')
                        gitea.start()
                    else:
                        transition.verify_environment()
                        business_config.verify(read_file(gitea, '/data/gitea/conf/app.ini')[0])
                        # 这是开放写入的唯一边界。此后任何启动/清理异常都不能回退已接收的新数据。
                        progress(task, 'publish', '隔离验证通过，正在开放目标服务', 98, target.name)
                        execute(helper, ['sh', '-ec', f'echo verified > {stage}/verified'], '记录已验证的发布边界')
                        journal.save('发布')
                        try:
                            transition.commit()
                            gitea.start()
                            target._restore_api_reader = None
                            from services.commit_service import _require_target_health
                            _require_target_health(target)
                            business_config.verify(read_file(gitea, '/data/gitea/conf/app.ini')[0])
                        except Exception as exc:
                            raise RuntimeError('隔离验证通过，但开放目标服务未确认成功；保留当前数据及回退点，不自动覆盖可能已接收的新数据：' + str(exc)) from exc
                    progress(task, 'cleanup', '正在清理暂存文件与释放恢复锁', 99, target.name)
                    execute(helper,['rm','-rf',stage],'清理文件暂存区')
                    sql(['rm','-rf',pg_temp],'清理数据库暂存区')
                    execute(helper, ['rm', '-rf', '/data/.manager-restore-lock'], '释放实例恢复锁')
                    finish_current_step(task, '暂存文件已清理，恢复锁已释放')
                finally:
                    try:
                        transition.close()
                        helper.remove(force=True)
                    finally: client.close()
            finalize.api_get_json = isolated.api_get_json
            finalize.snapshot_check = snapshot_check if snapshot is not None else None
            return finalize
        except Exception as primary:
            if report: report.fail(primary)
            if not getattr(primary, 'operation_stage', None): primary.operation_stage = task.progress_stage
            try:
                if isolated is not None: isolated.close()
                progress(task, 'rollback', '恢复失败，正在回退原数据', task.progress_percent or 0, str(primary))
                if swapped:
                    transition.current.stop(timeout=30)
                    stopped = True
                    execute(helper, ['sh','-ec', f'if test -d {stage}/old-gitea; then rm -rf /data/gitea; mv {stage}/old-gitea /data/gitea; fi; if test -d {stage}/old-repositories; then rm -rf /data/git/repositories; mv {stage}/old-repositories /data/git/repositories; fi; if test -d {stage}/old-lfs; then rm -rf /data/git/lfs; mv {stage}/old-lfs /data/git/lfs; fi'], '回退原文件')
                if swapped and artifacts:
                    execute(helper, ['sh', '-ec', rollback_command(stage, artifacts)], '回退 Actions 制品正文')
                if db_changed:
                    reset_database(pg_temp+'/original.sql', '回退原数据库')
                    gitea = transition.rollback()
                    restore_ssh(helper, stage)
                if stopped:
                    journal.value['新令牌'] = journal.value['原令牌']
                    journal.value['已回退'] = True
                    journal.save('发布')
                    gitea.start(); stopped = False
                finish_current_step(task, '原数据已回退，目标服务已启动')
                rollback_complete = True
            except Exception as rollback:
                raise RuntimeError(f'恢复失败：{primary}；自动回退未完成：{rollback}；回退点保留于 {stage} 和 {pg_temp}，目标服务保持停止') from primary
            raise
        finally:
            if helper is not None and not success:
                try:
                    if locked and rollback_complete:
                        execute(helper, ['rm', '-rf', '/data/.manager-restore-lock'], '释放实例恢复锁')
                finally: helper.remove(force=True)
                transition.close()
