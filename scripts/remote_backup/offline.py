"""源主机关闭、新数据库 Manager 导入与恢复；上传保持流式。"""
import json
import time
from pathlib import Path

import requests

from .driver import CheckFailure
from .network import control
from . import oracle
from .payload import MultipartArchive


def verify(lab):
    from .scenarios import download_backup
    lab.login()
    with lab.case('offline-preflight', '源离线恢复的三份新增归档及临时副本空间'):
        # 新备份、独立下载和冷 Manager 导入均保留，不能只预算单次临时解压。
        lab.ensure_space(future_backups=3)
    expected = oracle.snapshot(lab, 'a')
    backup = lab.backup('a')
    path = download_backup(lab, backup['id'])
    old_session, old_url = lab.manager, lab.manager_url
    old_id = lab.state['nodes']['b']['server_id']
    cold_started = source_stopped = False
    with lab.case('offline', '源主机关闭且独立 Manager 数据库为空时恢复'):
        try:
            control(lab, 'source-power', node='a', enabled=False); source_stopped = True
            control(lab, 'cold-manager', enabled=True); cold_started = True
            lab.manager_url = 'http://cold-backend:5000/api'
            lab.manager = requests.Session(); lab.manager.trust_env = False
            lab.login()
            if lab.manager_api('GET', '/backups').json() or lab.manager_api('GET', '/servers').json():
                raise CheckFailure('冷恢复 Manager 不是空数据库，不能证明独立导入')
            lab.manager_api('POST', '/settings', json={'host_ip': lab.state['addresses']['manager']})
            target = lab.state['nodes']['b']
            server = lab.manager_api('POST', '/servers', json={'name': '冷恢复目标', 'role': 'backup',
                'host': lab.state['addresses']['b'], 'ssh_port': 22, 'ssh_user': 'root',
                'gitea_url': lab.gitea_url('b'), 'gitea_port': 3000, 'gitea_container': 'gitea',
                'pg_container': 'postgres', 'pg_user': 'gitea', 'pg_dbname': 'gitea', 'api_token': target['token'],
                'connection_mode': 'ssh'}).json()
            boundary = 'remote-lab-stream-boundary'
            imported = lab.manager_api('POST', '/backups/import', data=MultipartArchive(path, boundary),
                headers={'Content-Type': 'multipart/form-data; boundary=' + boundary}, timeout=1800).json()
            target['server_id'] = server['id']
            result = lab.restore('b', imported['id'])
            if result['status'] != 'success':
                raise CheckFailure('独立导入后的恢复失败：' + result.get('error_msg', ''))
            differences = oracle.compare(expected, oracle.snapshot(lab, 'b'))
            evidence = lab.root / ('offline-comparison-' + str(time.time_ns()) + '.json')
            evidence.write_text(json.dumps({'说明': '源离线且新数据库导入恢复', '差异': differences}, ensure_ascii=False, indent=2))
            if differences:
                raise CheckFailure('独立恢复仍有业务差异，详见 ' + evidence.name)
        finally:
            lab.state['nodes']['b']['server_id'] = old_id
            lab.manager, lab.manager_url = old_session, old_url
            lab.save()
            if cold_started:
                control(lab, 'cold-manager', enabled=False)
            if source_stopped:
                control(lab, 'source-power', node='a', enabled=True)
                lab.wait_gitea('a')
            lab.login()
            lab.manager_api('PUT', '/servers/%s' % old_id,
                            json={'api_token': lab.state['nodes']['b']['token']})
