#!/usr/bin/env python3
"""自动发现同产品实例，选择后双重确认，只清除选中实例。"""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import contextlib
import sys

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
ROOT = SCRIPT_ROOT
sys.path.insert(0, str(ROOT / 'backend/services'))
from diagnostics import Report

LABEL = 'com.docker.compose.project'
VOLUMES = {
    'gitea': {'database-data', 'gitea-data'},
    'manager': {'manager-data', 'review-data', 'executor-data', 'review-workspaces', 'diagnostic-data'},
}


def identity(root):
    for name in ('state', 'compose.install.json', 'installer-manifest.json'):
        if (root / name).is_symlink():
            raise RuntimeError('拒绝处理符号链接：' + name)
    product = json.loads((root / 'installer-manifest.json').read_text())['product']
    if product not in VOLUMES:
        raise RuntimeError('安装包产品身份无效')
    path = root / 'state/installation.json'
    if path.is_symlink():
        raise RuntimeError('安装身份文件不能是符号链接')
    if not path.exists():
        if (root / 'compose.install.json').exists():
            raise RuntimeError('缺少实例身份但存在部署配置，拒绝猜测要删除的资源')
        return product, None
    state = json.loads(path.read_text())
    project = state.get('project', '')
    if (state.get('product') != product or state.get('directory') != str(root)
            or not re.fullmatch('gitea-' + product + r'-[0-9a-f]{10}', project)):
        raise RuntimeError('实例身份或安装目录不匹配，拒绝删除')
    return product, project


class Cleaner:
    def __init__(self, report, product, project):
        self.report, self.product, self.project = report, product, project

    def docker(self, *args):
        # 固定本机系统套接字，不接受远端上下文和环境覆盖。
        env = {k: v for k, v in os.environ.items() if not k.startswith(('DOCKER_', 'COMPOSE_'))}
        return self.report.command(['docker', '--host', 'unix:///var/run/docker.sock', *args],
                                   label='卸载资源检查或清理', timeout=30, env=env)[1].strip()

    def discover(self):
        # 只读取定位字段；原目录中的安装身份还需与项目标签交叉核验。
        fields = ('.ID', '.Names', '.Ports', '.Status',
                  '(.Label "com.docker.compose.project")',
                  '(.Label "com.docker.compose.project.working_dir")')
        template = '[' + ','.join('{{json ' + field + '}}' for field in fields) + ']'
        rows = self.docker('ps', '-a', '--format', template).splitlines()
        result = []
        for row in rows:
            cid, name, ports, status, project, directory = json.loads(row)
            if (project or '').startswith('gitea-' + self.product) or name.startswith('gitea-' + self.product):
                result.append({'容器': cid, '名称': name, '端口': ports, '状态': status,
                               '实例': project, '原安装目录': directory})
        return result

    def inventory(self):
        result = {}
        for kind, command in [('container', ('ps', '-aq')), ('volume', ('volume', 'ls', '-q')),
                              ('network', ('network', 'ls', '-q'))]:
            ids = self.docker(*command, '--filter', 'label=' + LABEL + '=' + self.project).split()
            result[kind] = sorted(ids)
        for name in result['volume']:
            if name not in {self.project + '_' + v for v in VOLUMES[self.product]}:
                raise RuntimeError('发现未知数据卷，拒绝删除：' + name)
            users = self.docker('ps', '-aq', '--filter', 'volume=' + name).split()
            if set(users) - set(result['container']):
                raise RuntimeError('数据卷被其他实例引用，拒绝删除：' + name)
        return result

    @contextlib.contextmanager
    def fresh_manager_guard(self, plan):
        # 写锁覆盖容器删除，防止检查后页面插入服务器并开始管理操作。
        connection = None
        try:
            volume = self.project + '_manager-data'
            if self.product == 'manager' and volume in plan['volume']:
                mount = Path(self.docker('volume', 'inspect', '--format', '{{.Mountpoint}}', volume))
                if not mount.is_absolute() or not mount.is_dir():
                    raise RuntimeError('无法访问本机 Manager 数据卷，拒绝卸载')
                database = mount / 'gitea-manager.db'
                if database.is_symlink():
                    raise RuntimeError('Manager 数据库不能是符号链接')
                connection = sqlite3.connect(database.as_uri() + '?mode=rwc', uri=True, timeout=3)
                connection.execute('BEGIN EXCLUSIVE')
                tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ('gitea_servers', 'restore_tasks', 'backups', 'scheduled_tasks'):
                    if table in tables and connection.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone():
                        raise RuntimeError('Manager 已有管理或业务记录；本脚本只用于首次安装失败，不允许中断管理任务。')
            yield
        finally:
            if connection:
                connection.close()

    def remove(self, plan):
        if self.inventory() != plan:
            raise RuntimeError('确认期间资源发生变化，请重新运行卸载')
        with self.fresh_manager_guard(plan):
            self.remove_kind('container', plan)
        for kind in ('volume', 'network'):
            self.remove_kind(kind, plan)
        if any(self.inventory().values()):
            raise RuntimeError('仍有实例资源残留，请查看诊断并重新运行')

    def remove_kind(self, kind, plan):
        for item in plan[kind]:
            # 删除前再次核对所有权，且不强制删除数据卷或网络。
            template = '{{index .Config.Labels "' + LABEL + '"}}' if kind == 'container' else '{{index .Labels "' + LABEL + '"}}'
            if self.docker(kind, 'inspect', '--format', template, item) != self.project:
                raise RuntimeError('资源归属改变，停止清理：' + item)
            args = [kind, 'rm'] + (['-f'] if kind == 'container' else []) + [item]
            self.docker(*args)

def confirm(product, project, ask=input):
    print('警告：将永久清空当前 ' + product + ' 实例的全部数据、账号、备份和密钥，无法恢复。')
    print('Manager 卸载不会删除其管理的 Gitea；Docker、离线镜像及安装文件保留。')
    if product == 'manager':
        print('仅支持首次安装失败且无服务器、备份、恢复或调度记录的 Manager 实例。')
    if ask('第一次确认，输入 UNINSTALL 继续（其他输入取消）：').strip() != 'UNINSTALL':
        return False
    phrase = 'DELETE ' + (project or product + '-bootstrap')
    return ask('第二次确认，请完整输入 ' + phrase + '：').strip() == phrase


def choose_target(root, product, project, cleaner, discovered, ask=input):
    if project:
        cleaner.project = project
        if any(cleaner.inventory().values()):
            return root, project
    candidates = sorted({row['实例'] for row in discovered
                         if re.fullmatch('gitea-' + product + r'-[0-9a-f]{10}', row['实例'] or '')})
    if not candidates:
        raise RuntimeError('未找到可确认归属的安装实例；未删除任何资源，不能报告卸载成功。')
    for number, candidate in enumerate(candidates, 1):
        rows = [row for row in discovered if row['实例'] == candidate]
        print(str(number) + '. ' + candidate + ' 端口：' + ', '.join(sorted({row['端口'] for row in rows if row['端口']})))
    if len(candidates) == 1:
        selected = candidates[0]
        print('自动识别到唯一实例：' + selected)
    else:
        choice = ask('发现多个实例，请输入要卸载的序号（其他输入取消）：').strip()
        if not choice.isdecimal() or not 1 <= int(choice) <= len(candidates):
            raise RuntimeError('已取消实例选择，未删除数据')
        selected = candidates[int(choice) - 1]
    directories = {row['原安装目录'] for row in discovered if row['实例'] == selected and row['原安装目录']}
    if len(directories) != 1:
        raise RuntimeError('选中实例的原安装目录缺失或不一致，不能安全清理其凭据；未删除数据')
    directory = Path(directories.pop())
    if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
        raise RuntimeError('自动定位的原安装目录不可用，未删除数据')
    directory = directory.resolve()
    if identity(directory) != (product, selected):
        raise RuntimeError('容器归属与自动定位的安装记录不一致，未删除数据')
    return directory, selected


def main():
    os.umask(0o077)
    if os.geteuid() != 0:
        raise RuntimeError('请使用 sudo bash uninstall.sh')
    report = Report(ROOT / 'uninstall-diagnostics', '卸载选中的安装实例')
    try:
        product, project = identity(ROOT)
        cleaner = Cleaner(report, product, project)
        with report.stage('自动发现安装实例'):
            discovered = cleaner.discover()
            report.write('discovered-containers.json', discovered)
            target, project = choose_target(ROOT, product, project, cleaner, discovered)
            cleaner.project = project
        with contextlib.ExitStack() as locks:
            if target != ROOT:
                lifecycle = locks.enter_context((target / '.lifecycle.lock').open('a'))
                fcntl.flock(lifecycle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = target / 'state'
            state.mkdir(mode=0o700, exist_ok=True)
            lock = locks.enter_context((state / 'install.lock').open('a'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if identity(target) != (product, project):
                raise RuntimeError('获取锁后实例身份发生变化，未删除数据')
            with report.stage('核对实例归属与删除范围'):
                plan = cleaner.inventory()
                if not any(plan.values()):
                    raise RuntimeError('选中实例已经没有 Docker 资源；未执行卸载，不报告成功')
                report.write('resources.json', {'产品': product, '实例': project, '原安装目录': str(target), '资源': plan})
                print('自动定位的安装目录：' + str(target))
                print('将卸载的实例：' + project)
                print(json.dumps([row for row in discovered if row['实例'] == project], ensure_ascii=False, indent=2))
                print(json.dumps(plan, ensure_ascii=False, indent=2))
            if not confirm(product, project):
                report.finish('用户取消，未删除数据')
                print('已取消，未删除数据。')
                return
            with report.stage('删除选中实例资源'):
                if identity(target) != (product, project):
                    raise RuntimeError('实例身份已改变，停止卸载')
                cleaner.remove(plan)
                remaining = cleaner.discover()
                report.write('remaining-containers.json', remaining)
                original_ids = set(plan['container'])
                if any(row['容器'] in original_ids or row['实例'] == project for row in remaining):
                    raise RuntimeError('卸载后发现目标容器残留，保留安装记录并停止报告成功')
            with report.stage('清除原安装状态与凭据'):
                (target / 'compose.install.json').unlink(missing_ok=True)
                shutil.rmtree(state)
        report.finish('卸载完成')
        print('已删除目标实例：' + project)
        if remaining:
            print('本机仍有其他同产品实例，未删除；重新安装前请核对其端口。')
        else:
            print('卸载完成：目标实例资源已删除，未发现其他同产品容器。')
    except (Exception, KeyboardInterrupt) as exc:
        report.fail(exc)
        report.finish('卸载未完成，保留剩余状态供重试')
        print('卸载未完成：' + str(exc), file=sys.stderr)
        return 1
    finally:
        print('卸载诊断（不含业务数据及密钥）：' + str(report.directory))
    return 0


if __name__ == '__main__':
    sys.exit(main())
