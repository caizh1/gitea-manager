"""每次打包都用固定前端镜像执行导航回归，并保存独立验收记录。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import socket
import tempfile
import time
from urllib.request import ProxyHandler, build_opener
from image_identity import archive_image_ids


ROOT = Path(__file__).resolve().parents[1]


def source_hashes():
    paths = list((ROOT / 'frontend/src').rglob('*'))
    paths.extend(ROOT / name for name in (
        'frontend/tests/routeRoots.test.mjs', 'frontend/package.json',
        'frontend/package-lock.json', 'scripts/test_route_navigation_ui.cjs',
        'deployment/frontend_release_gate.py', 'deployment/image_identity.py',
        'scripts/review-package.py', 'deployment/build-installers.py',
    ))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths) if path.is_file()}


def verify_archive(path, report):
    """导出的归档必须仍包含本次受测镜像，源码及测试也必须保持一致。"""
    archive_image_ids(path, {'frontend': {'tag': report['镜像'], 'ids': [report['镜像ID']]}})
    if source_hashes() != report['源码与测试SHA256']:
        raise RuntimeError('打包期间前端源码或导航测试发生变化，请重新执行')


def verify_frontend(image):
    """验证本次选定的不可变镜像；失败时抛出异常，打包入口必须停止。"""
    work = ROOT / 'work'
    work.mkdir(exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='frontend-release-', dir=work))
    report = {'状态': '执行中', '镜像': image, '开始时间UTC': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              '说明': '固定镜像、真实页面与路由、隔离接口、后台浏览器；每次重新执行，不复用历史通过记录。'}
    container = None

    def command(args, *, cwd=ROOT, env=None, timeout=120, log=None):
        result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        if log:
            (output / log).write_text('验收命令输出\n' + result.stdout + result.stderr, encoding='utf-8')
        if result.returncode:
            raise RuntimeError('导航发版检查未通过：' + ' '.join(args[:3]) + '；证据目录：' + str(output))
        return result.stdout

    try:
        before = source_hashes()
        command(['npm', 'run', 'test:route-roots'], cwd=ROOT / 'frontend', log='roots.log')
        info = json.loads(command(['docker', 'image', 'inspect', '--platform', 'linux/amd64', image]))[0]
        if info['Architecture'] != 'amd64' or info['Os'] != 'linux':
            raise RuntimeError('前端发版镜像必须为 Linux amd64')
        report['镜像ID'] = info['Id']
        # Docker containerd 的平台检查返回子清单 ID，启动使用本地可寻址的索引 ID。
        local_id = json.loads(command(['docker', 'image', 'inspect', image]))[0]['Id']
        # 选择宿主机可用端口，避免多个 Colima 实例各自分配同一个来宾端口。
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            host_port = probe.getsockname()[1]
        container = command([
            'docker', 'run', '-d', '--rm', '--platform', 'linux/amd64',
            '--label', 'io.gitea-manager.frontend-release-test=true',
            '--read-only', '--tmpfs', '/tmp:rw,mode=1777',
            '--tmpfs', '/var/cache/nginx:rw,mode=1777', '--tmpfs', '/var/run:rw,mode=1777',
            '-p', f'127.0.0.1:{host_port}:80', '--pull', 'never', local_id,
        ]).strip()
        runtime = json.loads(command(['docker', 'inspect', container]))[0]
        binding = runtime['NetworkSettings']['Ports']['80/tcp'][0]
        base = 'http://127.0.0.1:' + binding['HostPort']
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 20
        while True:
            try:
                with opener.open(base, timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError('隔离前端镜像未启动')
            time.sleep(.2)

        environment = os.environ.copy()
        environment.update(NAVIGATION_BASE_URL=base, NAVIGATION_OUTPUT_DIR=str(output))
        command(['node', str(ROOT / 'scripts/test_route_navigation_ui.cjs')],
                env=environment, timeout=600, log='navigation.log')
        navigation = json.loads((output / 'image.json').read_text())
        if (navigation.get('状态') != '通过' or not navigation.get('场景')
                or any(row.get('结果') != '通过' for row in navigation['场景'])
                or any(navigation.get(key) for key in ('浏览器异常', '动画告警', '未覆盖接口'))):
            raise RuntimeError('导航验收结果不完整或含异常')
        if source_hashes() != before:
            raise RuntimeError('导航验收期间源码或测试发生变化，请重新打包')
        report.update(状态='通过', 源码与测试SHA256=before, 导航验收=navigation)
    except Exception as error:
        report.update(状态='失败', 原因=str(error))
        raise
    finally:
        try:
            if container:
                try:
                    logs = subprocess.run(['docker', 'logs', container], capture_output=True, text=True, timeout=30)
                    (output / 'container.log').write_text('隔离前端容器日志\n' + logs.stdout + logs.stderr, encoding='utf-8')
                finally:
                    command(['docker', 'rm', '-f', container], timeout=30)
        except Exception as error:
            report.update(状态='失败', 清理错误=str(error))
            raise
        finally:
            (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print('导航发版检查证据：' + str(output), flush=True)
    print('导航发版检查通过：' + report['镜像ID'], flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='本次要打包的前端镜像')
    verify_frontend(parser.parse_args().image)
