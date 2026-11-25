"""依据 Docker 实际归属卸载 Manager；不读取或删除原安装目录。"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re

SERVICES = {'backend', 'frontend', 'review-worker', 'review-feedback', 'review-progress',
            'review-executor', 'review-pr-agent', 'approval-view', 'svn-migration-worker', 'git-migration-worker'}
LABEL = 'com.docker.compose.project'
UPGRADE_REVIEW_LABEL = 'io.gitea-manager.review-data'
# 与 upgrade_runtime.py 生成的独立卷及其固定挂载位置一致。
UPGRADE_VOLUMES = {'review-data': '/app/review',
                   'review-workspaces': '/app/review-workspaces',
                   'review-native': '/app/review-native',
                   'pr-agent-channels': '/app/pr-agent-channels'}
# 旧版直接运行 Compose，使用目录名或显式项目名；新版安装器附加随机后缀。
PROJECT_PATTERN = re.compile(r'gitea-manager(?:[-_][a-z0-9_-]+)?')


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
            mounts.append({'容器': cid, '类型': mount['Type'], '来源': mount.get('Name') or mount['Source'], '目标': mount['Destination']})
    upgrade_names = {cleaner.project + '-' + suffix: target for suffix, target in UPGRADE_VOLUMES.items()}
    # 升级器创建的外部卷可能没有 Compose 项目标签；也纳入中断删除后遗留的已知卷。
    names = set(cleaner.docker('volume', 'ls', '--format', '{{.Name}}').splitlines())
    volumes = set(plan['volume']) | (names & upgrade_names.keys())
    for mount in mounts:
        if mount['类型'] == 'volume' and mount['来源'] not in volumes:
            raise RuntimeError('实例挂载了非本项目管理的数据卷，拒绝删除：' + mount['来源'])
    identities = {}
    for name in sorted(volumes):
        identity = cleaner.volume_identity(name)
        labels = identity['labels']
        if any(labels.get(key) not in (None, '', cleaner.project)
               for key in (LABEL, UPGRADE_REVIEW_LABEL)):
            raise RuntimeError('数据卷属于其他实例，拒绝删除：' + name)
        if name in upgrade_names:
            if any(m['类型'] == 'volume' and m['来源'] == name and m['目标'] != upgrade_names[name]
                   for m in mounts):
                raise RuntimeError('升级数据卷挂载位置不一致，拒绝删除：' + name)
        elif labels.get(LABEL) != cleaner.project:
            raise RuntimeError('数据卷归属不明确，拒绝删除：' + name)
        users = set(cleaner.docker('ps', '-aq', '--filter', 'volume=' + name).split())
        if users - set(plan['container']):
            raise RuntimeError('数据卷被其他实例引用，拒绝删除：' + name)
        identities[name] = identity
    plan['volume'] = sorted(volumes)
    for network in plan['network']:
        attached = json.loads(cleaner.docker('network', 'inspect', '--format', '{{json .Containers}}', network)) or {}
        if any(not any(cid.startswith(expected) for expected in plan['container']) for cid in attached):
            raise RuntimeError('网络被其他实例使用，拒绝卸载')
    return dict(plan, mounts=sorted(mounts, key=lambda r: (r['容器'], r['目标'])),
                volume_identities=identities)


def run(cleaner, report, ask=input, lock_root=Path('/run/lock')):
    rows = cleaner.discover()
    # 容器已删、卷或网络删除失败时，仍可重新选择本项目收尾。
    for kind in ('volume', 'network'):
        raw = cleaner.docker(kind, 'ls', '--filter', 'label=' + LABEL,
                             '--format', '{{.Name}} {{.Label "' + LABEL + '"}}')
        for line in raw.splitlines():
            name, _, project = line.partition(' ')
            if PROJECT_PATTERN.fullmatch(project):
                rows.append({'实例':project,'名称':name,'状态':'项目资源','端口':''})
    raw = cleaner.docker('volume', 'ls', '--filter', 'label=' + UPGRADE_REVIEW_LABEL,
                         '--format', '{{.Name}} {{.Label "' + UPGRADE_REVIEW_LABEL + '"}}')
    for line in raw.splitlines():
        name, _, project = line.partition(' ')
        if PROJECT_PATTERN.fullmatch(project):
            rows.append({'实例': project, '名称': name, '状态': '升级数据卷', '端口': ''})
    for name in cleaner.docker('volume', 'ls', '--format', '{{.Name}}').splitlines():
        for suffix in UPGRADE_VOLUMES:
            ending = '-' + suffix
            project = name[:-len(ending)] if name.endswith(ending) else ''
            if PROJECT_PATTERN.fullmatch(project):
                rows.append({'实例': project, '名称': name, '状态': '升级数据卷', '端口': ''})
                break
    projects = sorted({r['实例'] for r in rows if PROJECT_PATTERN.fullmatch(r['实例'] or '')})
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
        if any(inventory(cleaner).values()):
            raise RuntimeError('仍有目标资源残留，卸载未完成，请保留诊断后重试')
        print('Manager 实例卸载完成：' + cleaner.project)
        print('宿主机绑定目录及旧安装文件均保留；如含敏感数据，请按实际挂载清单另行处理。')
        return True
