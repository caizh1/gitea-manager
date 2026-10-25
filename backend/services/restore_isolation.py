"""恢复验证只运行于无发布端口、仅回环监听的独立容器。"""
import ipaddress
import json
import logging
import time
from urllib.parse import urlencode

from docker.types import IPAMConfig, IPAMPool
from docker.errors import NotFound
from services.restore_secrets import put_file, read_file, normalized_environment

# 官方版本与当前增强版的定时任务；验证实例不执行删除、迁移恢复或镜像同步。
CRON_TASKS = '''stop_zombie_tasks stop_endless_tasks cancel_abandoned_jobs start_schedule_tasks cleanup_actions
governance_ssh_key_files governance_invitations governance_expired_grants governance_repository_deletion
governance_group_deletion governance_resource_cleanup governance_repository_creation_recovery
governance_mirror_operation_recovery governance_reference_business_recovery governance_audit_exports governance_audit_streams
update_mirrors repo_health_check check_repo_stats archive_cleanup sync_external_users deleted_branches_cleanup
update_migration_poster_id cleanup_hook_task_table cleanup_packages sync_repo_licenses delete_inactive_accounts
delete_repo_archives git_gc_repos resync_all_sshkeys resync_all_sshprincipals resync_all_hooks reinit_missing_repos
delete_missing_repos delete_generated_repository_avatars delete_old_actions update_checker delete_old_system_notices
gc_lfs rebuild_issue_indexer'''.split()


def available_ipam(client):
    occupied = []
    for network in client.networks.list():
        for config in (network.attrs.get('IPAM') or {}).get('Config') or []:
            if config.get('Subnet'):
                occupied.append(ipaddress.ip_network(config['Subnet']))
    subnet = next((subnet for subnet in ipaddress.ip_network('10.254.0.0/16').subnets(new_prefix=28)
                   if not any(subnet.overlaps(other) for other in occupied if other.version == 4)), None)
    if subnet is None:
        raise RuntimeError('没有可用的独立网段，尚未开放服务')
    return IPAMConfig(pool_configs=[IPAMPool(subnet=str(subnet))])


def retire_network(network, database):
    """Docker 断开响应早于网络端点视图收敛；网络回收不能阻断已停止写入后的回退。"""
    try:
        database.reload()
        if network.name in database.attrs['NetworkSettings']['Networks']:
            network.disconnect(database)
        deadline = time.monotonic() + 10
        while True:
            network.reload()
            if not network.attrs.get('Containers'):
                network.remove()
                return True
            if time.monotonic() >= deadline:
                logging.warning('验证进程已退出，临时网络端点仍在收敛，保留带事务标签的网络：%s', network.name)
                return False
            time.sleep(0.1)
    except NotFound:
        return True
    except Exception as exc:
        logging.warning('验证进程已退出，临时网络回收未完成；不阻断数据回退：%s (%s)', network.name, type(exc).__name__)
        return False


class IsolatedGitea:
    def __init__(self, client, original, database, server, marker):
        self.client, self.original, self.database, self.server = client, original, database, server
        self.marker = marker
        self.network = self.container = None

    def start(self):
        self.network = self.client.networks.create(self.marker, internal=True,
            ipam=available_ipam(self.client), labels={'gitea.manager.operation': self.marker})
        self.network.connect(self.database, aliases=['manager-verify-db'])
        environment = normalized_environment(dict(value.split('=', 1) for value in self.original.attrs['Config'].get('Env', []) if '=' in value))
        overrides = {'database__HOST': 'manager-verify-db:5432', 'server__HTTP_ADDR': '127.0.0.1',
            'server__HTTP_PORT': '3000', 'server__PROTOCOL': 'http', 'server__ROOT_URL': 'http://127.0.0.1:3000/',
            'server__REDIRECT_OTHER_PORT': 'false', 'server__START_SSH_SERVER': 'false', 'server__DISABLE_SSH': 'true',
            'mailer__ENABLED': 'false', 'mirror__ENABLED': 'false', 'actions__ENABLED': 'false',
            'service__DISABLE_REGISTRATION': 'true', 'log__MODE': 'console'}
        for name in CRON_TASKS:
            overrides['cron.' + name + '__ENABLED'] = 'false'
            overrides['cron.' + name + '__RUN_AT_START'] = 'false'
        # 清除同名的大小写变体及文件覆盖，防止覆盖回环监听和独立数据库地址。
        for key in list(environment):
            if any(key.upper() in ('GITEA__'+name.upper(), 'GITEA__'+name.upper()+'__FILE') for name in overrides):
                del environment[key]
        environment.update({'GITEA__'+key: value for key, value in overrides.items()})
        self.container = self.client.containers.create(self.original.attrs['Image'],
            name=self.marker+'-verify', entrypoint=['sh', '-ec'],
            command=['environment-to-ini --config /tmp/manager-verify.ini; exec /usr/local/bin/gitea web --config /tmp/manager-verify.ini'],
            user='git', environment=environment, volumes_from=[self.original.id], network=self.network.name,
            labels={'gitea.manager.operation': self.marker})
        raw, _ = read_file(self.original, '/data/gitea/conf/app.ini')
        put_file(self.container, '/tmp/manager-verify.ini', raw, 0o600, 1000, 1000)
        self.container.start()
        deadline = time.monotonic() + 120
        while True:
            try:
                self.api_get_json('/api/v1/version', timeout=3, token='')
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError('隔离 Gitea 启动检查失败，未开放业务端口') from None
                time.sleep(1)

    def api_get_json(self, path, params=None, timeout=30, token=None):
        from services.commit_service import RestoreValidationError
        if not path.startswith('/api/') or '?' in path:
            raise ValueError('隔离验证只允许固定 API 路径')
        url = 'http://127.0.0.1:3000' + path
        if params: url += '?' + urlencode(params)
        args = ['wget', '-q', '-T', str(timeout), '-O', '-']
        token = self.server.api_token if token is None else token
        if token: args += ['--header=Authorization: token ' + token]
        code, output = self.container.exec_run(args + [url], user='git')
        if code:
            raise RestoreValidationError('隔离验证接口暂不可用：' + path, retryable=True)
        try:
            return json.loads(output)
        except ValueError:
            raise RestoreValidationError('隔离验证接口没有返回有效 JSON：' + path) from None

    def close(self):
        # 必须先确认验证进程退出，才能回退数据库或开放原实例。
        if self.container is not None:
            self.container.stop(timeout=60)
            self.container.remove()
            self.container = None
        if self.network is not None:
            if retire_network(self.network, self.database):
                self.network = None
