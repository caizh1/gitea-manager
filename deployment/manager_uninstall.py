"""依据 Docker 实际归属卸载 Manager；不读取或删除原安装目录。"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re

SERVICES = {'backend', 'frontend', 'review-worker', 'review-feedback', 'review-progress',
            'review-executor', 'approval-view', 'svn-migration-worker'}
LABEL = 'com.docker.compose.project'


def inventory(cleaner):
    plan = cleaner.inventory()
    mounts = []
    for cid in plan['container']:
        # 避免输出 inspect 的环境变量、令牌等敏感字段。
        raw = cleaner.docker('container', 'inspect', '--format',
            '{{json .Config.Labels}}\n{{json .Mounts}}', cid).splitlines()
        labels, attached = map(json.loads, raw)
        if labels.get(LABEL) != cleaner.project or labels.get('com.docker.compose.service') not in SERVICES:
            raise RuntimeError('发现非预期服务，拒绝扩大卸载范围')
        for mount in attached:
            if mount['Type'] == 'volume' and mount.get('Name') not in plan['volume']:
                raise RuntimeError('实例挂载了非本项目管理的数据卷，拒绝删除：' + mount.get('Name', ''))
            mounts.append({'容器': cid, '类型': mount['Type'], '来源': mount.get('Name') or mount['Source'], '目标': mount['Destination']})
    for network in plan['network']:
        attached = json.loads(cleaner.docker('network', 'inspect', '--format', '{{json .Containers}}', network)) or {}
        if any(not any(cid.startswith(expected) for expected in plan['container']) for cid in attached):
            raise RuntimeError('网络被其他实例使用，拒绝卸载')
    return dict(plan, mounts=sorted(mounts, key=lambda r: (r['容器'], r['目标'])))


def run(cleaner, report, ask=input, lock_root=Path('/run/lock')):
    rows = cleaner.discover()
    # 容器已删、卷或网络删除失败时，仍可重新选择本项目收尾。
    for kind in ('volume', 'network'):
        raw = cleaner.docker(kind, 'ls', '--filter', 'label=' + LABEL,
                             '--format', '{{.Name}} {{.Label "' + LABEL + '"}}')
        for line in raw.splitlines():
            name, _, project = line.partition(' ')
            if re.fullmatch(r'gitea-manager-[0-9a-f]{10}', project):
                rows.append({'实例':project,'名称':name,'状态':'项目资源','端口':''})
    projects = sorted({r['实例'] for r in rows if re.fullmatch(r'gitea-manager-[0-9a-f]{10}', r['实例'] or '')})
    if not projects:
        raise RuntimeError('未找到 Manager 实例，未删除资源；不处理 Gitea 或 Runner')
    for i, project in enumerate(projects, 1):
        print(f'{i}. {project}')
        for row in rows:
            if row['实例'] == project:
                print('  ' + row['名称'] + ' ' + row['状态'] + ' ' + row['端口'])
    choice = '1' if len(projects) == 1 else ask('请选择 Manager 实例序号（其他输入取消）：').strip()
    if not choice.isdecimal() or not 1 <= int(choice) <= len(projects):
        raise RuntimeError('已取消选择，未删除任何资源')
    cleaner.project = projects[int(choice)-1]
    lock_root.mkdir(parents=True, exist_ok=True)
    path = lock_root / ('gitea-manager-uninstall-' + cleaner.project + '.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = inventory(cleaner)
        report.write('resources.json', plan)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        print('警告：将强制停止上述 Manager 服务，删除列出的项目数据卷及其中的账号、备份、审查记录和密钥，无法撤销。请先保存所需备份，停止正在执行的管理任务。')
        print('保留 Gitea、数据库、Runner、Docker、镜像以及所有宿主机绑定目录。不会删除或读取旧 /tmp 安装目录。')
        if ask('第一次确认：输入 UNINSTALL（其他输入取消）：').strip() != 'UNINSTALL':
            return False
        phrase = 'DELETE ' + cleaner.project
        if ask('第二次确认：完整输入 ' + phrase + '：').strip() != phrase:
            return False
        if inventory(cleaner) != plan:
            raise RuntimeError('确认期间资源或挂载发生变化，未执行删除，请重新运行')
        # 对每一步重新核对归属；共享卷及网络检查在所有容器删除之前完成。
        for kind in ('container', 'volume', 'network'):
            cleaner.remove_kind(kind, plan)
        if any(cleaner.inventory().values()):
            raise RuntimeError('仍有目标资源残留，卸载未完成，请保留诊断后重试')
        print('Manager 实例卸载完成：' + cleaner.project)
        print('宿主机绑定目录及旧安装文件均保留；如含敏感数据，请按实际挂载清单另行处理。')
        return True
