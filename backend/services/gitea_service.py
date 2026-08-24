import os
import time
import logging
import shlex
import requests
import hashlib
import re
from datetime import datetime
from models import db, GiteaServer, Backup, RestoreTask, Setting, get_setting
from services.ssh_service import SSHService
from services.docker_service import local_exec, local_cp_from, local_cp_to
from services.restore_progress import update_restore_progress
from services.remote_job_service import RemoteJobError, run_remote_job
from services.restore_step_service import (
    finish_restore_step,
    start_restore_step,
    update_restore_step,
)
from config import RESTORE_DIAGNOSTIC_INTERVAL_SECONDS, RESTORE_LONG_JOB_TIMEOUT_SECONDS
from config import BACKUP_DIR


def _ensure_url(url):
    if not url.startswith(('http://', 'https://')):
        url = 'http://' + url
    return url


def _parse_disk_usage(server, raw_df_output, raw_du_output=None):
    try:
        lines = raw_df_output.strip().split('\n')
        if lines and lines[0]:
            parts = lines[0].split()
            if len(parts) >= 6:
                total = int(parts[1]) * 1024
                used = int(parts[2]) * 1024
                if raw_du_output:
                    du_parts = raw_du_output.strip().split()
                    if du_parts:
                        used = int(du_parts[0]) * 1024
                server.disk_usage = f'{fmt_bytes(used)} / {fmt_bytes(total)}'
    except Exception:
        pass


def fmt_bytes(b):
    if b < 1048576: return f'{b/1024:.0f}K'
    if b < 1073741824: return f'{b/1048576:.1f}M'
    return f'{b/1073741824:.1f}G'


def _compact_output(text, limit=1200):
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    half = max(200, limit // 2)
    return text[:half] + '\n... <output truncated> ...\n' + text[-half:]


def _format_command_error(label, exit_code, stdout='', stderr=''):
    return (
        f'{label} (exit={exit_code})\n'
        f'stdout:\n{_compact_output(stdout)}\n'
        f'stderr:\n{_compact_output(stderr)}'
    )


def _limit_error_msg(message, limit=2000):
    message = str(message or '')
    if len(message) <= limit:
        return message
    half = limit // 2
    return message[:half] + '\n... <error truncated> ...\n' + message[-half:]


def _set_backup_snapshot_state(backup, status, repo_count=None, error='', collected_at=None):
    backup.commit_snapshot_status = status
    if repo_count is not None:
        backup.commit_snapshot_repo_count = repo_count
    backup.commit_snapshot_error = _limit_error_msg(error) if error else ''
    backup.commit_snapshot_collected_at = collected_at


def _collect_backup_commit_snapshot(backup_id):
    backup = Backup.query.get(backup_id)
    if not backup:
        raise Exception('Backup not found')

    logging.info('[备份] 开始采集 Commit ID 快照 - backup_id=%d', backup_id)
    _set_backup_snapshot_state(backup, 'running', repo_count=0, error='', collected_at=None)
    db.session.commit()

    try:
        from services.commit_service import collect_backup_commits
        repo_count = collect_backup_commits(backup_id)
    except Exception as e:
        db.session.rollback()
        backup = Backup.query.get(backup_id)
        if backup:
            _set_backup_snapshot_state(backup, 'failed', repo_count=0, error=str(e), collected_at=None)
            db.session.commit()
        raise Exception(f'Commit ID 快照采集失败: {e}')

    backup = Backup.query.get(backup_id)
    if not backup:
        raise Exception('Backup not found')
    _set_backup_snapshot_state(
        backup,
        'success',
        repo_count=repo_count,
        error='',
        collected_at=datetime.utcnow(),
    )
    logging.info('[备份] Commit ID 快照采集完成 - backup_id=%d repos=%d', backup_id, repo_count)
    return repo_count


GITEA_DATA_PERMISSION_CMD = 'chown -R git:git /data/gitea /data/git'


def _repair_gitea_data_permissions_local(server):
    logging.info('[permission] repairing Gitea data ownership in local container: %s', server.gitea_container)
    exit_code, out = local_exec(server.gitea_container, ['sh', '-c', GITEA_DATA_PERMISSION_CMD])
    if exit_code != 0:
        raise Exception(_format_command_error('repair gitea data permissions failed', exit_code, stdout=out))


def _repair_gitea_data_permissions_remote(server, ssh):
    logging.info('[permission] repairing Gitea data ownership in remote container: %s', server.gitea_container)
    cmd = (
        f'docker exec {shlex.quote(server.gitea_container)} '
        f'sh -c {shlex.quote(GITEA_DATA_PERMISSION_CMD)}'
    )
    exit_code, out, err = ssh.exec(cmd)
    if exit_code != 0:
        raise Exception(_format_command_error('repair gitea data permissions failed', exit_code, out, err))


def _repair_stopped_gitea_data_permissions_remote(server, ssh):
    logging.info('[permission] repairing Gitea data ownership via temporary container: %s', server.gitea_container)
    inspect_cmd = f"docker inspect -f '{{{{.Config.Image}}}}' {shlex.quote(server.gitea_container)}"
    exit_code, out, err = ssh.exec(inspect_cmd)
    if exit_code != 0 or not out.strip():
        raise Exception(_format_command_error('inspect gitea container image failed', exit_code, out, err))

    image = out.strip().splitlines()[-1]
    repair_cmd = (
        f'docker run --rm --user root --volumes-from {shlex.quote(server.gitea_container)} '
        f'--entrypoint sh {shlex.quote(image)} -c {shlex.quote(GITEA_DATA_PERMISSION_CMD)}'
    )
    exit_code, out, err = ssh.exec(repair_cmd)
    if exit_code != 0:
        raise Exception(_format_command_error('repair gitea data permissions failed', exit_code, out, err))


def fetch_server_info(server):
    headers = {'Authorization': f'token {server.api_token}'}

    try:
        resp = requests.get(f'{_ensure_url(server.gitea_url)}/api/v1/version', headers=headers, timeout=10)
        if resp.status_code == 200:
            server.version = resp.json().get('version', '')

        resp = requests.get(f'{_ensure_url(server.gitea_url)}/api/v1/user', headers=headers, timeout=10)
        if resp.status_code == 200:
            server.user_count = 1

        resp = requests.get(f'{_ensure_url(server.gitea_url)}/api/v1/admin/users', headers=headers, timeout=10)
        if resp.status_code == 200:
            server.user_count = len(resp.json())

        resp = requests.get(f'{_ensure_url(server.gitea_url)}/api/v1/repos/search', headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            server.repo_count = data.get('data', []) and len(data.get('data', [])) or 0
    except Exception:
        pass

    try:
        if server.is_local:
            from services.docker_service import _get_client
            client = _get_client()
            ct = client.containers.get(server.gitea_container)
            _, out = ct.exec_run(['sh', '-c', 'df -kP /data | tail -n +2'])
            df_raw = out.decode('utf-8', errors='replace').strip()
            du_raw = None
            try:
                _, du_out = ct.exec_run(['sh', '-c', 'du -sk /data 2>/dev/null'])
                du_raw = du_out.decode('utf-8', errors='replace').strip()
            except Exception:
                pass
            _parse_disk_usage(server, df_raw, du_raw)
        else:
            ssh = SSHService(server.host, server.ssh_port, server.ssh_user)
            _, out, _ = ssh.exec(f'docker exec {server.gitea_container} sh -c "df -kP /data | tail -n +2"')
            df_raw = out.strip()
            du_raw = None
            try:
                _, du_out, _ = ssh.exec(f'docker exec {server.gitea_container} sh -c "du -sk /data 2>/dev/null"')
                du_raw = du_out.strip()
            except Exception:
                pass
            _parse_disk_usage(server, df_raw, du_raw)
    except Exception:
        pass

    server.last_check_at = datetime.utcnow()
    db.session.commit()


def test_server_connection(server):
    import socket

    host_ip = get_setting('host_ip', '')
    if not host_ip:
        return False, '请先在「系统设置」中配置本机IP'

    try:
        target_ip = socket.gethostbyname(server.host)
    except Exception:
        target_ip = None

    is_local = target_ip == host_ip
    logging.info('[连接检测] configured_host_ip=%s target_ip=%s is_local=%s', host_ip, target_ip, is_local)

    server.is_local = is_local
    db.session.commit()

    if is_local:
        try:
            exit_code, out = local_exec(server.gitea_container, 'echo ok')
            if exit_code == 0 and 'ok' in out:
                resp = requests.get(f'{_ensure_url(server.gitea_url)}/api/v1/version',
                                   headers={'Authorization': f'token {server.api_token}'},
                                   timeout=10)
                if resp.status_code == 200:
                    return True, 'ok (本地 Docker)'
                return False, f'Gitea API 返回 {resp.status_code}'
            return False, f'Docker exec 失败 (exit={exit_code})'
        except Exception as e:
            logging.warning('[连接检测] 本地 Docker 失败: %s，尝试 SSH', e)

    try:
        ssh = SSHService(server.host, server.ssh_port, server.ssh_user)
        if not ssh.test_connection():
            logging.error(f'[{server.name}] SSH test failed to {server.host}:{server.ssh_port}')
            return False, 'SSH 连接失败，请检查主机地址、端口和 SSH 密钥'

        resp = requests.get(f'{_ensure_url(server.gitea_url)}/api/v1/version',
                           headers={'Authorization': f'token {server.api_token}'},
                           timeout=10)
        if resp.status_code != 200:
            logging.error(f'[{server.name}] Gitea API returned {resp.status_code}')
            return False, f'Gitea API 返回 {resp.status_code}'
        server.is_local = is_local
        return True, 'ok (远程 SSH)'
    except Exception as e:
        logging.error(f'[{server.name}] Connection test error: {e}', exc_info=True)
        return False, str(e)


def _backup_local(server, backup):
    container_tmp = f'/tmp/gitea-manager-backup-{backup.id}-{backup.filename}'
    local_path = os.path.join(BACKUP_DIR, backup.filename)
    os.makedirs(BACKUP_DIR, exist_ok=True)

    try:
        _repair_gitea_data_permissions_local(server)
        local_exec(server.gitea_container, f'rm -f "{container_tmp}"')
        dump_cmd = f'/usr/local/bin/gitea dump -c /data/gitea/conf/app.ini --tempdir /tmp -f "{container_tmp}"'
        logging.info('[备份-本地] 执行: gitea dump ...')
        exit_code, out = local_exec(server.gitea_container, dump_cmd, user='git', workdir='/tmp')
        if exit_code != 0:
            raise Exception(_format_command_error('gitea dump failed', exit_code, stdout=out))

        exit_code, out = local_exec(server.gitea_container, f'test -s "{container_tmp}" && echo ok')
        if exit_code != 0 or 'ok' not in out:
            raise Exception(_format_command_error(
                'dump exited successfully but backup file is missing or empty',
                exit_code,
                stdout=out,
            ))

        logging.info('[备份-本地] 提取文件到: %s', local_path)
        local_cp_from(server.gitea_container, container_tmp, local_path)
    finally:
        try:
            local_exec(server.gitea_container, f'rm -f "{container_tmp}"')
        except Exception:
            logging.warning('[备份-本地] 清理临时文件失败: %s', container_tmp, exc_info=True)

    file_size = os.path.getsize(local_path)
    backup.file_size = file_size
    backup.file_path = local_path
    logging.info('[备份-本地] 完成 - 大小: %d 字节', file_size)


def _backup_remote(server, backup):
    ssh = SSHService(server.host, server.ssh_port, server.ssh_user)

    remote_tmp = f'/tmp/gitea-manager-backup-{backup.id}-{backup.filename}'
    container_tmp = f'/tmp/gitea-manager-backup-{backup.id}-{backup.filename}'
    cleanup_cmds = [
        f'docker exec {server.gitea_container} rm -f "{container_tmp}"',
        f'rm -f "{remote_tmp}"',
    ]
    try:
        _repair_gitea_data_permissions_remote(server, ssh)
        for cmd in cleanup_cmds:
            ssh.exec(cmd)

        dump_cmd = (
            f'docker exec -u git -w /tmp {server.gitea_container} '
            f'/usr/local/bin/gitea dump -c /data/gitea/conf/app.ini --tempdir /tmp -f "{container_tmp}"'
        )
        logging.info('[备份-远程] 执行: gitea dump ...')
        exit_code, out, err = ssh.exec(dump_cmd)
        if exit_code != 0:
            raise Exception(_format_command_error('gitea dump failed', exit_code, out, err))

        exit_code, out, err = ssh.exec(f'docker exec {server.gitea_container} test -s "{container_tmp}"')
        if exit_code != 0:
            raise Exception(_format_command_error(
                'dump exited successfully but backup file is missing or empty',
                exit_code,
                out,
                err,
            ))

        cp_cmd = f'docker cp {server.gitea_container}:"{container_tmp}" "{remote_tmp}"'
        logging.info('[备份-远程] 拷贝文件到宿主机 ...')
        exit_code, out, err = ssh.exec(cp_cmd)
        if exit_code != 0:
            raise Exception(_format_command_error('docker cp failed', exit_code, out, err))

        os.makedirs(BACKUP_DIR, exist_ok=True)
        local_path = os.path.join(BACKUP_DIR, backup.filename)
        logging.info('[备份-远程] 下载文件到本地: %s', local_path)
        ssh.get_file(remote_tmp, local_path)
    finally:
        for cmd in cleanup_cmds:
            try:
                ssh.exec(cmd)
            except Exception:
                logging.warning('[备份-远程] 清理临时文件失败: %s', cmd, exc_info=True)

    file_size = os.path.getsize(local_path)
    backup.file_size = file_size
    backup.file_path = local_path
    logging.info('[备份-远程] 完成 - 大小: %d 字节', file_size)


def do_backup(backup_id):
    backup = Backup.query.get(backup_id)
    if not backup:
        return

    server = GiteaServer.query.get(backup.source_server_id)
    if not server:
        backup.status = 'failed'
        backup.error_msg = 'Server not found'
        backup.completed_at = datetime.utcnow()
        db.session.commit()
        return

    logging.info('[备份] 开始 - 源: %s (%s) is_local=%s', server.name, server.host, server.is_local)
    backup.status = 'running'
    backup.error_msg = ''
    _set_backup_snapshot_state(backup, 'pending', repo_count=0, error='', collected_at=None)
    db.session.commit()

    try:
        if server.is_local:
            try:
                _backup_local(server, backup)
            except Exception as e:
                logging.warning('[备份] 本地失败 (%s)，回退到远程模式', e)
                _backup_remote(server, backup)
        else:
            _backup_remote(server, backup)
        _collect_backup_commit_snapshot(backup_id)
        backup = Backup.query.get(backup_id)
        if backup:
            backup.status = 'success'
            backup.error_msg = ''
            backup.completed_at = datetime.utcnow()
    except Exception as e:
        backup = Backup.query.get(backup_id)
        if not backup:
            return
        backup.status = 'failed'
        backup.error_msg = _limit_error_msg(str(e))
        if backup.commit_snapshot_status in ('', 'pending', 'running'):
            _set_backup_snapshot_state(backup, 'failed', repo_count=0, error=str(e), collected_at=None)
        backup.completed_at = datetime.utcnow()
        logging.error('[备份] 失败 - %s', e)

    db.session.commit()


def _restore_local(target, backup, task):
    import zipfile, tarfile, io, shutil, docker as dk

    local_work = f'/tmp/restore_work_{backup.id}'
    os.makedirs(local_work, exist_ok=True)
    update_restore_progress(task, 'extract', '正在解压备份包', 10, backup.filename)

    logging.info('[恢复-本地] 解压备份包...')
    with zipfile.ZipFile(backup.file_path, 'r') as zf:
        zf.extractall(local_work)

    dump_root = local_work
    if not os.path.exists(os.path.join(dump_root, 'gitea-db.sql')):
        for root, dirs, files in os.walk(local_work):
            if 'gitea-db.sql' in files:
                dump_root = root
                break

    client = dk.from_env()
    container = client.containers.get(target.gitea_container)
    image = container.attrs['Config']['Image']

    logging.info('[恢复-本地] 停止目标 Gitea ...')
    update_restore_progress(task, 'stop_gitea', '正在停止目标 Gitea 服务', 20, target.name)
    container.stop()

    dump_tar = f'/tmp/dump_{backup.id}.tar'
    with tarfile.open(dump_tar, 'w') as tar:
        tar.add(dump_root, arcname='dump')
    with open(dump_tar, 'rb') as f:
        dump_data = f.read()
    os.unlink(dump_tar)

    logging.info('[恢复-本地] 创建临时容器并覆盖数据 ...')
    update_restore_progress(task, 'copy_files', '正在覆盖仓库和配置文件', 40, target.name)
    temp = client.containers.run(
        image=image,
        command='sleep 300',
        volumes_from=[target.gitea_container],
        detach=True,
    )
    try:
        ts = io.BytesIO(dump_data)
        temp.put_archive('/tmp', ts)

        copy_cmd = (
            'rm -rf /data/gitea /data/git/repositories && '
            'mkdir -p /data/gitea /data/git/repositories /data/gitea/conf && '
            '(cp -a /tmp/dump/data/. /data/gitea/ 2>/dev/null || true) && '
            '(cp -a /tmp/dump/repos/. /data/git/repositories/ 2>/dev/null || true) && '
            '(cp /tmp/dump/app.ini /data/gitea/conf/app.ini 2>/dev/null || true)'
        )
        exit_code, out = temp.exec_run(f'sh -c "{copy_cmd}"')
        if exit_code != 0:
            raise Exception(f'Copy files failed (exit={exit_code})\n{out.decode("utf-8", errors="replace")[:500]}')

        logging.info('[restore-local] Repairing Gitea data permissions ...')
        update_restore_progress(task, 'fix_permissions', '正在修复 Gitea 数据目录权限', 82, target.name)
        exit_code, out = temp.exec_run('chown -R git:git /data')
        out = out.decode('utf-8', errors='replace')
        if exit_code != 0:
            raise Exception(_format_command_error('repair gitea data permissions failed', exit_code, stdout=out))
    finally:
        temp.stop(timeout=2)
        temp.remove(force=True)

    sql_file = os.path.join(dump_root, 'gitea-db.sql')
    if os.path.exists(sql_file):
        update_restore_progress(task, 'wait_database', '正在等待 PostgreSQL 就绪', 55, target.pg_container)
        pg_container = client.containers.get(target.pg_container)
        for i in range(30):
            pg_container.reload()
            if pg_container.status == 'running':
                break
            logging.info('[恢复-本地] PG 容器状态: %s，等待...', pg_container.status)
            time.sleep(2)
        else:
            raise Exception('PostgreSQL 容器无法启动')

        logging.info('[恢复-本地] 等待 PostgreSQL 稳定就绪 (最长5分钟) ...')
        deadline = time.time() + 300
        pg_ready = False
        while time.time() < deadline:
            ec, _ = pg_container.exec_run(f'pg_isready -U {target.pg_user}')
            if ec != 0:
                time.sleep(5)
                continue
            time.sleep(10)
            ec2, _ = pg_container.exec_run(f'pg_isready -U {target.pg_user}')
            if ec2 != 0:
                time.sleep(5)
                continue
            time.sleep(5)
            ec3, _ = pg_container.exec_run(f'psql -U {target.pg_user} -d postgres -c "SELECT 1"')
            if ec3 == 0:
                pg_ready = True
                break
            time.sleep(5)

        if not pg_ready:
            raise Exception('PostgreSQL 5 分钟内未能稳定启动。目标 Gitea 服务器可能完全不可用，请立即检查目标服务器！')

        logging.info('[恢复-本地] 重建数据库 ...')
        update_restore_progress(task, 'recreate_database', '正在重建目标数据库', 68, target.pg_dbname)
        pg_container.exec_run(f'dropdb -U {target.pg_user} --if-exists {target.pg_dbname}')
        pg_container.exec_run(f'createdb -U {target.pg_user} -O {target.pg_user} {target.pg_dbname}')

        logging.info('[恢复-本地] 导入数据库 ...')
        update_restore_progress(task, 'import_database', '正在导入备份数据库', 76, target.pg_dbname)
        ts = io.BytesIO()
        with tarfile.open(fileobj=ts, mode='w') as tar:
            tar.add(sql_file, arcname='restore.sql')
        ts.seek(0)
        pg_container.put_archive('/tmp', ts)
        exit_code, out = pg_container.exec_run(
            f'psql -U {target.pg_user} -d {target.pg_dbname} -f /tmp/restore.sql'
        )
        if exit_code != 0:
            raise Exception(f'Database import failed (exit={exit_code})\n{out.decode("utf-8", errors="replace")[:500]}')
        pg_container.exec_run('rm -f /tmp/restore.sql')

    logging.info('[恢复-本地] 启动目标 Gitea ...')
    update_restore_progress(task, 'start_gitea', '正在启动目标 Gitea 服务', 86, target.name)
    container.start()

    logging.info('[恢复-本地] 重新生成 hooks 和 keys ...')
    update_restore_progress(task, 'regenerate_hooks', '正在重新生成 hooks 和 keys', 90, target.name)
    container.exec_run('/usr/local/bin/gitea admin regenerate hooks', user='git')
    container.exec_run('/usr/local/bin/gitea admin regenerate keys', user='git')

    shutil.rmtree(local_work, ignore_errors=True)
    logging.info('[恢复-本地] 恢复命令完成')


REMOTE_NAME_PATTERN = re.compile(r'^[A-Za-z0-9_.-]+$')


def _checked_remote_name(value, label):
    value = str(value or '')
    if not value or not REMOTE_NAME_PATTERN.fullmatch(value):
        raise ValueError(f'{label} 包含不安全字符: {value!r}')
    return value


def _exception_message(exc):
    text = str(exc or '').strip()
    if text:
        return f'{type(exc).__name__}: {text}'
    return f'{type(exc).__name__}: {exc!r}'


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _require_ssh_command(ssh, command, label, timeout=600):
    exit_code, out, err = ssh.exec(command, timeout=timeout)
    if exit_code != 0:
        raise RuntimeError(_format_command_error(label, exit_code, out, err))
    return out, err


def _run_simple_restore_step(task, step_key, label, percent, detail, action):
    step = start_restore_step(task.id, step_key, label, detail=detail)
    update_restore_progress(task, step_key, label, percent, detail)
    started = time.monotonic()
    try:
        result = action()
        elapsed = max(0, int(time.monotonic() - started))
        finish_restore_step(
            step,
            status='success',
            detail=f'{detail}，完成于 {elapsed} 秒' if detail else f'完成于 {elapsed} 秒',
            exit_code=0,
            metrics={'耗时秒数': elapsed},
        )
        return result
    except Exception as exc:
        message = _exception_message(exc)
        finish_restore_step(
            step,
            status='failed',
            detail=message,
            exit_code=getattr(getattr(exc, 'result', None), 'exit_code', -1),
            stdout_tail=getattr(getattr(exc, 'result', None), 'stdout_tail', ''),
            stderr_tail=getattr(getattr(exc, 'result', None), 'stderr_tail', ''),
            metrics=getattr(getattr(exc, 'result', None), 'metrics', None),
        )
        raise


def _run_long_restore_step(
    task,
    ssh,
    remote_root,
    step_key,
    label,
    percent,
    detail,
    command,
    diagnostic_callback=None,
    timeout_callback=None,
):
    job_dir = f'{remote_root}/steps/{step_key}'
    step = start_restore_step(
        task.id,
        step_key,
        label,
        detail=detail,
        remote_job_dir=job_dir,
    )
    update_restore_progress(task, step_key, label, percent, detail)
    last_persisted = [-RESTORE_DIAGNOSTIC_INTERVAL_SECONDS]

    def on_progress(status, elapsed, metrics):
        if elapsed - last_persisted[0] < RESTORE_DIAGNOSTIC_INTERVAL_SECONDS:
            return
        state_text = metrics.get('远端状态', '正在运行')
        progress_detail = f'{detail}；已运行 {elapsed} 秒；远端状态 {state_text}'
        update_restore_progress(task.id, step_key, label, percent, progress_detail)
        update_restore_step(step.id, detail=progress_detail, metrics=metrics, remote_job_dir=job_dir)
        last_persisted[0] = elapsed

    try:
        result = run_remote_job(
            ssh,
            job_dir,
            command,
            timeout_seconds=RESTORE_LONG_JOB_TIMEOUT_SECONDS,
            progress_callback=on_progress,
            diagnostic_callback=diagnostic_callback,
            timeout_callback=timeout_callback,
        )
        finish_restore_step(
            step.id,
            status='success',
            detail=f'{detail}，完成于 {result.elapsed_seconds} 秒',
            exit_code=result.exit_code,
            stdout_tail=result.stdout_tail,
            stderr_tail=result.stderr_tail,
            metrics=result.metrics,
        )
        return result
    except Exception as exc:
        result = getattr(exc, 'result', None)
        finish_restore_step(
            step.id,
            status='failed',
            detail=_exception_message(exc),
            exit_code=getattr(result, 'exit_code', -1),
            stdout_tail=getattr(result, 'stdout_tail', ''),
            stderr_tail=getattr(result, 'stderr_tail', ''),
            metrics=getattr(result, 'metrics', None),
        )
        raise


def _remote_du_bytes(ssh, path):
    quoted_path = shlex.quote(path)
    out, _ = _require_ssh_command(
        ssh,
        f'du -sb {quoted_path} 2>/dev/null | cut -f1',
        f'读取目录大小失败: {path}',
        timeout=120,
    )
    try:
        return int(out.strip().splitlines()[-1])
    except Exception:
        return 0


def _verify_remote_archive(ssh, remote_zip, local_size, local_hash):
    quoted_zip = shlex.quote(remote_zip)
    probe = (
        f'stat -c "%s" {quoted_zip} && '
        f'sha256sum {quoted_zip} | cut -d " " -f1 && '
        f"unzip -l {quoted_zip} | awk 'END {{print $1}}' && "
        'df -Pk /tmp | awk \'END {printf "%.0f\\n", $4 * 1024}\''
    )
    out, _ = _require_ssh_command(
        ssh,
        f'sh -c {shlex.quote(probe)}',
        '校验远端备份包失败',
        timeout=RESTORE_LONG_JOB_TIMEOUT_SECONDS,
    )
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if len(lines) < 4:
        raise RuntimeError(f'远端备份校验输出不完整: {out!r}')
    remote_size = int(lines[0])
    remote_hash = lines[1]
    uncompressed_size = int(lines[2])
    available_bytes = int(lines[3])
    if remote_size != local_size or remote_hash.lower() != local_hash.lower():
        raise RuntimeError(
            f'备份上传校验不一致: local={local_size}/{local_hash}, '
            f'remote={remote_size}/{remote_hash}'
        )
    if available_bytes < uncompressed_size:
        raise RuntimeError(
            f'目标机 /tmp 空间不足: 可用 {available_bytes} 字节，解压至少需要 {uncompressed_size} 字节'
        )
    return {
        '远端字节': remote_size,
        '解压后字节': uncompressed_size,
        '/tmp可用字节': available_bytes,
        'SHA-256': remote_hash,
    }


def _restore_remote(target, backup, task):
    ssh = SSHService(target.host, target.ssh_port, target.ssh_user)
    gitea_container = _checked_remote_name(target.gitea_container, 'Gitea 容器名')
    pg_container = _checked_remote_name(target.pg_container, 'PostgreSQL 容器名')
    pg_user = _checked_remote_name(target.pg_user, 'PostgreSQL 用户名')
    pg_dbname = _checked_remote_name(target.pg_dbname, 'PostgreSQL 数据库名')

    remote_root = f'/tmp/gitea-manager/restore-{task.id}'
    remote_zip = f'{remote_root}/backup.zip'
    extract_dir = f'{remote_root}/extracted'
    sql_file = f'{extract_dir}/gitea-db.sql'
    quoted_root = shlex.quote(remote_root)
    quoted_zip = shlex.quote(remote_zip)
    quoted_extract = shlex.quote(extract_dir)

    def prepare_remote():
        out, _ = _require_ssh_command(
            ssh,
            'command -v docker && command -v unzip && command -v sha256sum && command -v setsid && '
            'mkdir -p /tmp/gitea-manager && '
            "find /tmp/gitea-manager -mindepth 1 -maxdepth 1 -type d -name 'restore-*' "
            f'-mtime +7 -exec rm -rf -- {{}} + && mkdir -p {quoted_root}/steps && '
            'df -Pk /tmp | tail -n 1',
            '目标机恢复前置检查失败',
            timeout=120,
        )
        return out

    _run_simple_restore_step(task, 'remote_prepare', '正在检查目标机恢复环境', 6, target.name, prepare_remote)

    local_size = os.path.getsize(backup.file_path)
    local_hash = _sha256_file(backup.file_path)
    upload_step = start_restore_step(task.id, 'upload_backup', '正在上传备份包', detail=backup.filename)
    update_restore_progress(task, 'upload_backup', '正在上传备份包到目标服务器', 10, backup.filename)
    upload_started = time.monotonic()
    upload_last = [0.0]

    def upload_progress(transferred, total):
        now = time.monotonic()
        if now - upload_last[0] < RESTORE_DIAGNOSTIC_INTERVAL_SECONDS and transferred < total:
            return
        percent = int(transferred * 100 / max(total, 1))
        metrics = {'已传输字节': transferred, '总字节': total, '上传百分比': percent}
        detail = f'{backup.filename}；{percent}%（{transferred}/{total} 字节）'
        update_restore_progress(task.id, 'upload_backup', '正在上传备份包到目标服务器', 10, detail)
        update_restore_step(upload_step.id, detail=detail, metrics=metrics)
        upload_last[0] = now

    try:
        ssh.put_file(backup.file_path, remote_zip, callback=upload_progress)
        elapsed = max(0, int(time.monotonic() - upload_started))
        finish_restore_step(
            upload_step.id,
            detail=f'{backup.filename}，上传完成于 {elapsed} 秒',
            metrics={'文件字节': local_size, '耗时秒数': elapsed, 'SHA-256': local_hash},
        )
    except Exception as exc:
        finish_restore_step(upload_step.id, status='failed', detail=_exception_message(exc), exit_code=-1)
        raise

    def verify_upload():
        return _verify_remote_archive(ssh, remote_zip, local_size, local_hash)

    verify_metrics = _run_simple_restore_step(
        task, 'verify_upload', '正在校验上传备份包', 15, backup.filename, verify_upload
    )
    verify_step = start_restore_step(task.id, 'upload_evidence', '备份包上传证据', detail='大小和 SHA-256 一致')
    finish_restore_step(verify_step, detail='大小和 SHA-256 一致', metrics=verify_metrics)

    extract_command = f'rm -rf {quoted_extract} && mkdir -p {quoted_extract} && unzip -o {quoted_zip} -d {quoted_extract}'
    _run_long_restore_step(
        task,
        ssh,
        remote_root,
        'extract',
        '正在解压备份包',
        25,
        backup.filename,
        extract_command,
    )

    def verify_extract():
        required = [
            sql_file,
            f'{extract_dir}/repos',
            f'{extract_dir}/data',
            f'{extract_dir}/app.ini',
        ]
        tests = ' && '.join(
            f'test -s {shlex.quote(path)}' if path.endswith(('.sql', '.ini'))
            else f'test -d {shlex.quote(path)}'
            for path in required
        )
        out, _ = _require_ssh_command(
            ssh,
            f'{tests} && du -sb {shlex.quote(sql_file)} {shlex.quote(extract_dir + "/repos")} '
            f'{shlex.quote(extract_dir + "/data")}',
            '备份解压内容不完整',
            timeout=300,
        )
        return out

    _run_simple_restore_step(task, 'verify_extract', '正在检查解压内容', 30, backup.filename, verify_extract)

    def stop_gitea():
        command = (
            f'if test "$(docker inspect -f "{{{{.State.Running}}}}" {shlex.quote(gitea_container)})" = true; '
            f'then docker stop {shlex.quote(gitea_container)}; else printf "already stopped\\n"; fi'
        )
        return _require_ssh_command(ssh, command, '停止目标 Gitea 失败', timeout=300)

    _run_simple_restore_step(task, 'stop_gitea', '正在停止目标 Gitea 服务', 35, target.name, stop_gitea)

    def recreate_database():
        drop_command = (
            f'docker exec {shlex.quote(pg_container)} dropdb -U {shlex.quote(pg_user)} '
            f'--if-exists --force {shlex.quote(pg_dbname)}'
        )
        create_command = (
            f'docker exec {shlex.quote(pg_container)} createdb -U {shlex.quote(pg_user)} '
            f'-O {shlex.quote(pg_user)} {shlex.quote(pg_dbname)}'
        )
        _require_ssh_command(ssh, drop_command, '删除旧 PostgreSQL 数据库失败', timeout=300)
        return _require_ssh_command(ssh, create_command, '创建 PostgreSQL 数据库失败', timeout=300)

    _run_simple_restore_step(
        task, 'recreate_database', '正在重建目标数据库', 42, pg_dbname, recreate_database
    )

    pg_app_name = f'gitea-manager-restore-{task.id}'
    import_command = (
        f'docker exec -e PGAPPNAME={shlex.quote(pg_app_name)} -i {shlex.quote(pg_container)} '
        f'psql -X -v ON_ERROR_STOP=1 -v VERBOSITY=terse '
        f'-U {shlex.quote(pg_user)} -d {shlex.quote(pg_dbname)} '
        f'< {shlex.quote(sql_file)}'
    )

    def database_diagnostics(status, elapsed):
        sql = (
            "SELECT pg_database_size('" + pg_dbname + "'), "
            "COALESCE((SELECT state || '|' || COALESCE(wait_event_type,'') || '|' || "
            "COALESCE(wait_event,'') FROM pg_stat_activity WHERE application_name='" + pg_app_name + "' "
            "ORDER BY backend_start DESC LIMIT 1), 'not-visible')"
        )
        command = (
            f'docker exec {shlex.quote(pg_container)} psql -X -U {shlex.quote(pg_user)} '
            f'-d postgres -At -F "|" -c {shlex.quote(sql)}'
        )
        exit_code, out, err = ssh.exec(command, timeout=30)
        metrics = {'SQL文件字节': _remote_du_bytes(ssh, sql_file)}
        if exit_code == 0 and out:
            parts = out.strip().split('|')
            metrics['数据库字节'] = int(parts[0]) if parts and parts[0].isdigit() else parts[0]
            metrics['PostgreSQL状态'] = '|'.join(parts[1:]) if len(parts) > 1 else 'unknown'
        else:
            metrics['PostgreSQL诊断'] = err or out or '命令失败'
        disk_probe = "df -Pk /var/lib/postgresql/data 2>/dev/null | tail -n 1 | awk '{print $4}'"
        disk_exit, disk_out, _ = ssh.exec(
            f'docker exec {shlex.quote(pg_container)} sh -c {shlex.quote(disk_probe)}',
            timeout=30,
        )
        if disk_exit == 0 and disk_out.strip().isdigit():
            metrics['PostgreSQL可用KB'] = int(disk_out.strip())
        return metrics

    def terminate_database_import():
        sql = (
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE application_name='{pg_app_name}' AND pid <> pg_backend_pid()"
        )
        command = (
            f'docker exec {shlex.quote(pg_container)} psql -X -U {shlex.quote(pg_user)} '
            f'-d postgres -At -c {shlex.quote(sql)}'
        )
        ssh.exec(command, timeout=60)

    _run_long_restore_step(
        task,
        ssh,
        remote_root,
        'import_database',
        '正在导入备份数据库',
        58,
        pg_dbname,
        import_command,
        diagnostic_callback=database_diagnostics,
        timeout_callback=terminate_database_import,
    )

    image_out, _ = _require_ssh_command(
        ssh,
        f'docker inspect -f "{{{{.Config.Image}}}}" {shlex.quote(gitea_container)}',
        '读取 Gitea 容器镜像失败',
        timeout=60,
    )
    gitea_image = image_out.strip().splitlines()[-1]

    def copy_diagnostics(destination, source):
        source_size = _remote_du_bytes(ssh, source)

        def collect(status, elapsed):
            copy_probe = (
                f'du -sb {destination} 2>/dev/null | cut -f1 || true; '
                f"df -Pk {destination} 2>/dev/null | tail -n 1 | awk '{{print $4}}'"
            )
            command = (
                f'docker run --rm --user root --volumes-from {shlex.quote(gitea_container)} '
                f'--entrypoint sh {shlex.quote(gitea_image)} -c '
                f'{shlex.quote(copy_probe)}'
            )
            exit_code, out, err = ssh.exec(command, timeout=120)
            target_size = 0
            lines = out.strip().splitlines()
            if exit_code == 0 and lines:
                try:
                    target_size = int(lines[0])
                except ValueError:
                    target_size = 0
            metrics = {
                '源目录字节': source_size,
                '目标目录字节': target_size,
                '目标大小诊断': '正常' if exit_code == 0 else (err or '失败'),
            }
            if len(lines) > 1 and lines[-1].isdigit():
                metrics['目标数据盘可用KB'] = int(lines[-1])
            return metrics

        return collect

    repos_source = f'{extract_dir}/repos'
    reset_repos = (
        f'docker run --rm --user root --volumes-from {shlex.quote(gitea_container)} '
        f'--entrypoint sh {shlex.quote(gitea_image)} -c '
        f'{shlex.quote("find /data/git/repositories -mindepth 1 -delete && mkdir -p /data/git/repositories")}'
    )
    repos_command = (
        f'{reset_repos} && docker cp {shlex.quote(repos_source + "/.")} '
        f'{shlex.quote(gitea_container + ":/data/git/repositories/")}'
    )
    _run_long_restore_step(
        task,
        ssh,
        remote_root,
        'copy_repos',
        '正在覆盖仓库文件',
        72,
        target.name,
        repos_command,
        diagnostic_callback=copy_diagnostics('/data/git/repositories', repos_source),
    )

    data_source = f'{extract_dir}/data'
    reset_data = (
        f'docker run --rm --user root --volumes-from {shlex.quote(gitea_container)} '
        f'--entrypoint sh {shlex.quote(gitea_image)} -c '
        f'{shlex.quote("find /data/gitea -mindepth 1 -delete && mkdir -p /data/gitea/conf")}'
    )
    data_command = (
        f'{reset_data} && docker cp {shlex.quote(data_source + "/.")} '
        f'{shlex.quote(gitea_container + ":/data/gitea/")}'
    )
    _run_long_restore_step(
        task,
        ssh,
        remote_root,
        'copy_data',
        '正在覆盖 Gitea 数据目录',
        78,
        target.name,
        data_command,
        diagnostic_callback=copy_diagnostics('/data/gitea', data_source),
    )

    def copy_config():
        command = (
            f'docker cp {shlex.quote(extract_dir + "/app.ini")} '
            f'{shlex.quote(gitea_container + ":/data/gitea/conf/app.ini")}'
        )
        return _require_ssh_command(ssh, command, '覆盖 app.ini 失败', timeout=300)

    _run_simple_restore_step(task, 'copy_config', '正在覆盖 Gitea 配置文件', 82, target.name, copy_config)

    permission_command = (
        f'docker run --rm --user root --volumes-from {shlex.quote(gitea_container)} '
        f'--entrypoint sh {shlex.quote(gitea_image)} -c '
        f'{shlex.quote(GITEA_DATA_PERMISSION_CMD)}'
    )
    _run_long_restore_step(
        task,
        ssh,
        remote_root,
        'fix_permissions',
        '正在修复 Gitea 数据目录权限',
        86,
        target.name,
        permission_command,
    )

    def start_gitea():
        return _require_ssh_command(
            ssh,
            f'docker start {shlex.quote(gitea_container)}',
            '启动目标 Gitea 失败',
            timeout=300,
        )

    _run_simple_restore_step(task, 'start_gitea', '正在启动目标 Gitea 服务', 89, target.name, start_gitea)

    hooks_command = (
        f'docker exec -u git {shlex.quote(gitea_container)} /usr/local/bin/gitea '
        f'-c /data/gitea/conf/app.ini admin regenerate hooks && '
        f'docker exec -u git {shlex.quote(gitea_container)} /usr/local/bin/gitea '
        f'-c /data/gitea/conf/app.ini admin regenerate keys'
    )
    _run_long_restore_step(
        task,
        ssh,
        remote_root,
        'regenerate_hooks',
        '正在重新生成 hooks 和 keys',
        92,
        target.name,
        hooks_command,
    )

    logging.info('[恢复-远程] 恢复命令完成 - task_id=%s remote_root=%s', task.id, remote_root)
    return remote_root


def _sync_restored_api_token(target, backup):
    source_token = (backup.source_api_token or '').strip()
    if not source_token:
        logging.warning(
            '[restore] backup %s has no source_api_token; target server API token is unchanged',
            backup.id,
        )
        return

    if target.api_token != source_token:
        target.api_token = source_token
        logging.info('[restore] synced target %s API token from backup snapshot', target.name)


def do_restore(task_id):
    task = RestoreTask.query.get(task_id)
    if not task:
        return

    backup = Backup.query.get(task.backup_id)
    target = GiteaServer.query.get(task.target_server_id)

    if not backup or not target:
        task.status = 'failed'
        task.error_msg = 'Backup or target server not found'
        task.completed_at = datetime.utcnow()
        update_restore_progress(task, 'failed', '恢复失败', 100, task.error_msg)
        db.session.commit()
        return

    if not os.path.exists(backup.file_path):
        task.status = 'failed'
        task.error_msg = 'Backup file not found'
        task.completed_at = datetime.utcnow()
        update_restore_progress(task, 'failed', '恢复失败', 100, task.error_msg)
        db.session.commit()
        return

    logging.info('[恢复] 开始 - 目标: %s (%s) is_local=%s, 备份: %s', target.name, target.host, target.is_local, backup.filename)
    update_restore_progress(task, 'prepare', '正在准备恢复任务', 5, backup.filename)
    remote_root = None
    try:
        if target.is_local:
            def run_local_restore():
                return _restore_local(target, backup, task)

            try:
                _run_simple_restore_step(
                    task,
                    'local_restore',
                    '正在执行本地恢复',
                    10,
                    target.name,
                    run_local_restore,
                )
            except Exception as e:
                logging.warning('[恢复] 本地失败 (%s)，回退到远程模式', e)
                remote_root = _restore_remote(target, backup, task)
        else:
            remote_root = _restore_remote(target, backup, task)

        def sync_token():
            _sync_restored_api_token(target, backup)
            db.session.commit()

        _run_simple_restore_step(
            task,
            'sync_token',
            '正在同步恢复后的 API Token',
            93,
            target.name,
            sync_token,
        )

        verify_step = start_restore_step(
            task.id,
            'verify_restore',
            '正在执行健康检查和 Commit ID 验证',
            detail=target.name,
        )
        update_restore_progress(
            task,
            'restore_done',
            '恢复命令完成，开始健康检查和 Commit ID 验证',
            94,
            target.name,
        )
        from services.commit_service import verify_restore
        verify_restore(task_id)

        task = RestoreTask.query.get(task_id)
        if task and task.status == 'success':
            finish_restore_step(
                verify_step.id,
                status='success',
                detail='健康检查和 Commit ID 验证通过',
                exit_code=0,
            )
        else:
            verification_error = task.error_msg if task else '恢复验证任务不存在'
            finish_restore_step(
                verify_step.id,
                status='failed',
                detail=verification_error,
                exit_code=-1,
                stderr_tail=verification_error,
            )

        if task and task.status == 'success' and remote_root:
            cleanup_ssh = SSHService(target.host, target.ssh_port, target.ssh_user)

            def cleanup_remote():
                return _require_ssh_command(
                    cleanup_ssh,
                    f'rm -rf -- {shlex.quote(remote_root)}',
                    '清理远端恢复临时目录失败',
                    timeout=300,
                )

            try:
                _run_simple_restore_step(
                    task,
                    'cleanup',
                    '正在清理恢复临时文件',
                    99,
                    remote_root,
                    cleanup_remote,
                )
            except Exception:
                logging.warning('[恢复] 成功后的临时目录清理失败 - %s', remote_root, exc_info=True)
            task = RestoreTask.query.get(task_id)
            if task and task.status == 'success':
                update_restore_progress(task, 'completed', '恢复完成，健康检查和 Commit ID 验证通过', 100, '')
    except Exception as e:
        db.session.rollback()
        task = RestoreTask.query.get(task_id)
        if not task:
            logging.exception('[恢复] 失败且任务记录不存在 - task_id=%s', task_id)
            return
        failed_stage = task.progress_stage or 'unknown'
        error_text = _limit_error_msg(f'阶段 {failed_stage}: {_exception_message(e)}')
        task.status = 'failed'
        task.error_msg = error_text
        task.completed_at = datetime.utcnow()
        update_restore_progress(task, 'failed', '恢复失败', 100, error_text[:500])
        logging.exception('[恢复] 失败 - task_id=%s stage=%s error=%s', task_id, failed_stage, error_text)

    db.session.commit()


def get_server_detail(server):
    result = {}
    bs = Backup.query.filter_by(source_server_id=server.id).order_by(Backup.started_at.desc()).limit(10).all()
    result['backups'] = [{
        'id': b.id, 'filename': b.filename, 'file_size': b.file_size,
        'status': b.status, 'started_at': b.started_at.isoformat() if b.started_at else None,
        'completed_at': b.completed_at.isoformat() if b.completed_at else None,
    } for b in bs]
    result['backup_count'] = Backup.query.filter_by(source_server_id=server.id).count()

    rs = RestoreTask.query.filter_by(target_server_id=server.id).order_by(RestoreTask.started_at.desc()).limit(10).all()
    result['restores'] = [{
        'id': r.id, 'backup_filename': r.backup.filename if r.backup else '',
        'status': r.status, 'error_msg': r.error_msg,
        'started_at': r.started_at.isoformat() if r.started_at else None,
        'completed_at': r.completed_at.isoformat() if r.completed_at else None,
    } for r in rs]
    result['restore_count'] = RestoreTask.query.filter_by(target_server_id=server.id).count()

    try:
        if server.is_local:
            import docker as dk
            client = dk.from_env()
            ct = client.containers.get(server.gitea_container)
            result['container'] = {
                'name': ct.name,
                'image': ct.attrs['Config']['Image'],
                'status': ct.status,
            }
            try:
                stats = ct.stats(stream=False)
                cpu = stats.get('cpu_stats', {}).get('cpu_usage', {}).get('total_usage', 0)
                system_cpu = stats.get('cpu_stats', {}).get('system_cpu_usage', 1) or 1
                mem_used = stats.get('memory_stats', {}).get('usage', 0)
                mem_limit = stats.get('memory_stats', {}).get('limit', 1) or 1
                result['resources'] = {
                    'cpu_percent': round(cpu / system_cpu * 100, 1),
                    'memory_used': mem_used,
                    'memory_limit': mem_limit,
                }
            except Exception:
                pass
            try:
                _, out = ct.exec_run(['sh', '-c', 'df -kP /data | tail -n +2'])
                df_raw = out.decode('utf-8', errors='replace').strip()
                result['disk'] = df_raw
                du_raw = None
                try:
                    _, du_out = ct.exec_run(['sh', '-c', 'du -sk /data 2>/dev/null'])
                    du_raw = du_out.decode('utf-8', errors='replace').strip()
                except Exception:
                    pass
                _parse_disk_usage(server, df_raw, du_raw)
            except Exception:
                pass
            try:
                result['logs'] = ct.logs(tail=80).decode('utf-8', errors='replace')[-3000:]
            except Exception:
                result['logs'] = ''
        else:
            ssh = SSHService(server.host, server.ssh_port, server.ssh_user)
            _, out, _ = ssh.exec(f'docker logs --tail 80 {server.gitea_container}')
            result['logs'] = out[-3000:]
            _, out, _ = ssh.exec(f'docker exec {server.gitea_container} sh -c "df -kP /data | tail -n +2"')
            df_raw = out.strip()
            result['disk'] = df_raw
            du_raw = None
            try:
                _, du_out, _ = ssh.exec(f'docker exec {server.gitea_container} sh -c "du -sk /data 2>/dev/null"')
                du_raw = du_out.strip()
            except Exception:
                pass
            _parse_disk_usage(server, df_raw, du_raw)
            _, out, _ = ssh.exec(f'docker inspect {server.gitea_container} --format "{{{{.Config.Image}}}}"')
            result['container'] = {'image': out.strip(), 'status': 'remote'}
    except Exception as e:
        result['detail_error'] = str(e)

    return result
