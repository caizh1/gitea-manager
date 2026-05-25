import os
import time
import logging
import shlex
import requests
from datetime import datetime
from models import db, GiteaServer, Backup, RestoreTask, Setting, get_setting
from services.ssh_service import SSHService
from services.docker_service import local_exec, local_cp_from, local_cp_to
from services.restore_progress import update_restore_progress
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
    backup.status = 'success'
    backup.completed_at = datetime.utcnow()
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
    backup.status = 'success'
    backup.completed_at = datetime.utcnow()
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
    try:
        if server.is_local:
            try:
                _backup_local(server, backup)
            except Exception as e:
                logging.warning('[备份] 本地失败 (%s)，回退到远程模式', e)
                _backup_remote(server, backup)
        else:
            _backup_remote(server, backup)
    except Exception as e:
        backup.status = 'failed'
        backup.error_msg = _limit_error_msg(str(e))
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
    task.status = 'success'
    task.completed_at = datetime.utcnow()
    logging.info('[恢复-本地] 完成')


def _restore_remote(target, backup, task):
    ssh = SSHService(target.host, target.ssh_port, target.ssh_user)

    remote_tmp_zip = f'/tmp/{backup.filename}'
    remote_tmp_dir = '/tmp/gitea_restore'

    logging.info('[恢复-远程] 上传备份文件...')
    update_restore_progress(task, 'upload_backup', '正在上传备份包到目标服务器', 10, backup.filename)
    ssh.put_file(backup.file_path, remote_tmp_zip)

    logging.info('[恢复-远程] 停止目标 Gitea ...')
    update_restore_progress(task, 'stop_gitea', '正在停止目标 Gitea 服务', 20, target.name)
    exit_code, out, err = ssh.exec(f'docker stop {target.gitea_container}')
    if exit_code != 0:
        raise Exception(f'docker stop failed (exit={exit_code})\nstdout: {out[:500]}\nstderr: {err[:500]}')

    logging.info('[恢复-远程] 解压备份包...')
    update_restore_progress(task, 'extract', '正在解压备份包', 30, backup.filename)
    ssh.exec(f'rm -rf {remote_tmp_dir}')
    exit_code, out, err = ssh.exec(f'mkdir -p {remote_tmp_dir} && unzip -o {remote_tmp_zip} -d {remote_tmp_dir}')
    if exit_code != 0:
        raise Exception(f'Unzip failed (exit={exit_code})\nstdout: {out[:500]}\nstderr: {err[:500]}')

    sql_file = f'{remote_tmp_dir}/gitea-db.sql'
    if ssh.file_exists(sql_file):
        logging.info('[恢复-远程] 重建数据库 ...')
        update_restore_progress(task, 'recreate_database', '正在重建目标数据库', 45, target.pg_dbname)
        ssh.exec(f'docker exec {target.pg_container} dropdb -U {target.pg_user} --if-exists {target.pg_dbname}')
        ssh.exec(f'docker exec {target.pg_container} createdb -U {target.pg_user} -O {target.pg_user} {target.pg_dbname}')

        logging.info('[恢复-远程] 导入数据库 ...')
        update_restore_progress(task, 'import_database', '正在导入备份数据库', 60, target.pg_dbname)
        sql_content_cmd = f'cat {sql_file}'
        exit_code, sql_content, err = ssh.exec(sql_content_cmd)
        if exit_code == 0 and sql_content:
            import_sql = (
                f'docker exec -i {target.pg_container} '
                f'psql -U {target.pg_user} -d {target.pg_dbname}'
            )
            c = ssh._get_client()
            try:
                stdin, stdout, stderr = c.exec_command(import_sql, timeout=300)
                stdin.write(sql_content)
                stdin.channel.shutdown_write()
                out = stdout.read().decode('utf-8', errors='replace')
                err_out = stderr.read().decode('utf-8', errors='replace')
                exit_code = stdout.channel.recv_exit_status()
            finally:
                c.close()

    logging.info('[恢复-远程] 覆盖 repos/ ...')
    update_restore_progress(task, 'copy_repos', '正在覆盖仓库文件', 72, target.name)
    ssh.exec(f'docker cp {remote_tmp_dir}/repos/. {target.gitea_container}:/data/git/repositories/')

    if ssh.file_exists(f'{remote_tmp_dir}/data'):
        logging.info('[恢复-远程] 覆盖 data/ ...')
        update_restore_progress(task, 'copy_data', '正在覆盖 Gitea 数据目录', 76, target.name)
        ssh.exec(f'docker cp {remote_tmp_dir}/data/. {target.gitea_container}:/data/gitea/data/')

    app_ini_src = f'{remote_tmp_dir}/app.ini'
    if ssh.file_exists(app_ini_src):
        logging.info('[恢复-远程] 覆盖 app.ini ...')
        update_restore_progress(task, 'copy_config', '正在覆盖 Gitea 配置文件', 80, target.name)
        ssh.exec(f'docker cp {app_ini_src} {target.gitea_container}:/data/gitea/conf/app.ini')

    logging.info('[restore-remote] Repairing Gitea data permissions ...')
    update_restore_progress(task, 'fix_permissions', '正在修复 Gitea 数据目录权限', 82, target.name)
    _repair_stopped_gitea_data_permissions_remote(target, ssh)

    logging.info('[恢复-远程] 启动目标 Gitea ...')
    update_restore_progress(task, 'start_gitea', '正在启动目标 Gitea 服务', 86, target.name)
    exit_code, out, err = ssh.exec(f'docker start {target.gitea_container}')
    if exit_code != 0:
        raise Exception(f'docker start failed (exit={exit_code})\nstdout: {out[:500]}\nstderr: {err[:500]}')

    logging.info('[恢复-远程] 重新生成 hooks 和 keys ...')
    update_restore_progress(task, 'regenerate_hooks', '正在重新生成 hooks 和 keys', 90, target.name)
    ssh.exec(f'docker exec -u git {target.gitea_container} /usr/local/bin/gitea admin regenerate hooks')
    ssh.exec(f'docker exec -u git {target.gitea_container} /usr/local/bin/gitea admin regenerate keys')

    cleanup_cmds = [f'rm -f {remote_tmp_zip}', f'rm -rf {remote_tmp_dir}']
    for cmd in cleanup_cmds:
        ssh.exec(cmd)

    task.status = 'success'
    task.completed_at = datetime.utcnow()
    logging.info('[恢复-远程] 完成')


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
    try:
        if target.is_local:
            try:
                _restore_local(target, backup, task)
            except Exception as e:
                logging.warning('[恢复] 本地失败 (%s)，回退到远程模式', e)
                _restore_remote(target, backup, task)
        else:
            _restore_remote(target, backup, task)
        if task.status == 'success':
            update_restore_progress(task, 'sync_token', '正在同步恢复后的 API Token', 92, target.name)
            _sync_restored_api_token(target, backup)
            update_restore_progress(task, 'restore_done', '恢复完成，等待 Commit ID 验证', 94, target.name)
    except Exception as e:
        task.status = 'failed'
        task.error_msg = str(e)
        task.completed_at = datetime.utcnow()
        update_restore_progress(task, 'failed', '恢复失败', 100, task.error_msg[:500])
        logging.error('[恢复] 失败 - %s', e)

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
