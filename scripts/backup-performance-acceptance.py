"""使用独立 Docker 引擎和真实调度入口验收全量备份性能。"""
import argparse
import json
import shutil

from remote_backup import host


def invoke(root, phase, workload):
    host.exec_service(root, 'driver', ['python', '-m', 'remote_backup.driver', phase, '--workload', workload])


def configure_speed(root, host_image):
    queues = {}
    for service in ('host-a', 'backend', 'driver'):
        container = host.service_id(root, service)
        attrs = host.assert_owned(root, container)
        if len(attrs['NetworkSettings']['Networks']) != 1:
            raise RuntimeError('需要确保传输仅走有整形的测试网卡')
        command = ['docker', 'run', '--rm', '--network', 'container:' + container, '--cap-add', 'NET_ADMIN',
                   '--entrypoint', 'sh', host_image, '-ec',
                   'tc qdisc replace dev eth0 root tbf rate 16mbit burst 64kb latency 200ms; '
                   'tc -j -s qdisc show dev eth0']
        queue = json.loads(host.run(command, capture=True).stdout)
        if not any(row.get('kind') == 'tbf' and row.get('options', {}).get('rate') == 2_000_000 for row in queue):
            raise RuntimeError('实测网络队列没有应用约定限速')
        queues[service] = queue
    host.write_json(root / 'performance-network-queues.json', queues)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name')
    parser.add_argument('--workload', choices=['small', 'full'], required=True)
    parser.add_argument('--image-prefix', default='gm-backup-perf')
    args = parser.parse_args()
    if not args.name.isascii() or not args.name.replace('-', '').isalnum() or not args.name[0].islower():
        parser.error('运行名需要由英文字母、数字或短横线组成')
    root = host.ROOT / 'work' / 'remote-backup' / args.name
    root.mkdir(parents=True, mode=0o700)
    pre = host.preflight(root)
    required = (240 if args.workload == 'full' else 30) * 1024 ** 3
    if pre['状态'] != '通过' or shutil.disk_usage(root).free < required:
        raise RuntimeError('隔离宿主机资源不足，尚未部署或开始正式计时')
    state = host.generate(root, pre, 'gitea/gitea:1.26.4')
    images = {name: {'名称': args.image_prefix + '-' + suffix, '摘要': host.image_id(args.image_prefix + '-' + suffix)}
              for name, suffix in [('manager', 'manager'), ('frontend', 'frontend'), ('host', 'host'), ('driver', 'driver')]}
    host.write_json(root / 'images.json', images)
    host.topology(root, state, shared_bridge=True)
    host.compose(root, 'up', '-d')
    host.trust_test_hosts(root, state)
    host.copy_images(root, state)
    host.driver(root, 'probe')
    engines = {service: host.exec_service(root, service, ['docker', 'info', '--format', '{{.ID}}'],
                                        capture=True).stdout.strip() for service in ('host-a', 'host-b')}
    if len(set(engines.values())) != 2:
        raise RuntimeError('源站与目标站须使用独立 Docker 引擎')
    host.write_json(root / 'performance-topology.json', {'引擎': engines, '测试网络': state['networks'][0]})
    host.driver(root, 'deploy')
    invoke(root, 'performance-seed', args.workload)
    if args.workload == 'full':
        configure_speed(root, images['host']['摘要'])
        invoke(root, 'performance-calibrate', args.workload)
    invoke(root, 'performance-run', args.workload)


if __name__ == '__main__':
    main()
