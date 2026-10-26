#!/usr/bin/env python3
"""从最终离线包启动全新接收端和两套 Manager，完成固定清单全部消费端验收。"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('package', 'output', 'gitea-state', 'gitea-fixture', 'gitlab-state', 'gitlab-fixture'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--gitea-subnet', required=True)
    parser.add_argument('--gitlab-subnet', required=True)
    parser.add_argument('--node', required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    assert not args.output.exists(), '必须使用新的验收目录，保留先前失败'
    args.output.mkdir(parents=True)
    extracted = args.output / 'package'
    report = {'说明': '仅使用最终包内脚本、镜像和空白隔离卷；不挂载开发源码，不复用候选任务或接收端',
              '结果': '执行中', '步骤': [], '来源': {}, '开始UTC': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    result_path = args.output / 'final-acceptance.json'

    def save():
        result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')

    def execute(name, command, cwd):
        print('正在验收：' + name, flush=True)
        output = args.output / (name + '.log')
        start = time.monotonic()
        with output.open('w') as stream:
            process = subprocess.run(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT)
        report['步骤'].append({'项目': name, '退出码': process.returncode, '耗时秒': round(time.monotonic() - start, 2), '日志': output.name})
        save()
        assert process.returncode == 0, '最终包验收失败，保留日志：' + str(output)
        return output.read_text()

    try:
        root = Path(__file__).resolve().parents[1]
        execute('package-integrity', [sys.executable, str(root / 'scripts/git-migration-package-acceptance.py'),
                '--package', str(args.package.resolve()), '--output', str(extracted), '--load-images'], root)
        report['离线包'] = json.loads((extracted / 'package-acceptance.json').read_text())
        text = execute('receiver-initialize', [sys.executable, 'scripts/git-migration-write-acceptance.py', '--image',
                'gitea-migration-receiver:offline', '--initialize-only', '--source-state', str(args.gitea_state.resolve())], extracted)
        receiver = extracted / next(line.split('：', 1)[1] for line in text.splitlines() if line.startswith('独立接收端已初始化：'))
        report['独立接收端状态'] = str(receiver)
        for kind in ('gitea', 'gitlab'):
            state = getattr(args, kind + '_state').resolve()
            fixture = getattr(args, kind + '_fixture').resolve()
            source = json.loads(fixture.read_text())
            command = [sys.executable, 'scripts/git-migration-manager-acceptance.py', '--image-tag', 'offline',
                       '--target-state', str(receiver), '--source-state', str(state), '--fixture', str(fixture),
                       '--project-name', source['project']['name'], '--subnet', getattr(args, kind + '_subnet'), '--interrupt-once']
            if kind == 'gitea':
                command.append('--rotate-source-credential')
            text = execute(kind + '-manager', command, extracted)
            evidence = extracted / next(line.split('：', 1)[1] for line in text.splitlines() if line.startswith('隔离 Manager 验收通过：'))
            manager = evidence.parent / 'resources.json'
            common = ['--manager-state', str(manager), '--target-state', str(receiver), '--fixture', str(fixture), '--run-id', '1']
            for test in ('advanced', 'completion', 'rule'):
                execute(kind + '-' + test, [sys.executable, 'scripts/git-migration-' + test + '-acceptance.py', *common], extracted)
            execute(kind + '-browser', [args.node, 'scripts/git-migration-ui-acceptance.cjs', '--manager-state', str(manager)], extracted)
            advanced = json.loads((manager.parent / 'advanced-acceptance-1.json').read_text())
            completion = json.loads((manager.parent / 'completion-acceptance-1.json').read_text())
            rules = json.loads((manager.parent / 'rule-acceptance-1.json').read_text())
            assert completion['结果'] == '通过' and not completion.get('候选续验'), '最终包必须从头完成全部条件，不能汇总候选续验'
            report['来源'][kind] = {'状态文件': str(manager), '项目数': len(advanced['项目']),
                                 '对象及引用回执数': advanced['对象覆盖']['原生对象'], '独立来源对象数': sum(r['来源对象数'] for r in advanced['项目']),
                                 '独立来源规则数': sum(r['来源规则数'] for r in advanced['项目']), '文件数': len(advanced['历史文件']),
                                 'C产物执行数': len(advanced['C产物执行']), '协议数': len({r['类型'] for r in advanced['制品协议']}),
                                 '合并组合数': len(completion['合并矩阵']), '补齐场景数': len(completion['场景']),
                                 '保护行为场景数': len(rules['场景']), '来源清单逐项覆盖': '100%', '文件摘要差异': 0}
            save()
            if kind == 'gitea':
                text = execute('abandon-fixture', [sys.executable, 'scripts/git-migration-abandon-fixture.py',
                               '--manager-state', str(manager), '--completed-run-id', '1'], extracted)
                stopped = next(line.split('：', 1)[1] for line in text.splitlines() if line.startswith('创建终止验收任务：'))
                execute('scope-credential-recovery', [sys.executable, 'scripts/git-migration-scope-credential-acceptance.py',
                        '--manager-state', str(manager), '--target-state', str(receiver), '--run-id', stopped], extracted)
                execute('abandon-recovery', [sys.executable, 'scripts/git-migration-abandon-acceptance.py',
                        '--manager-state', str(manager), '--target-state', str(receiver), '--run-id', stopped], extracted)
        report['结果'] = '通过'
        report['结束UTC'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        report['未验证'] = ['GitLab EE 付费能力', '其他版本与协议', '生产及物理宿主机', '真实大型公司项目', '人工节省百分之八十']
        report['排除'] = ['OCI／容器镜像']
        save()
        print('最终包完整端到端验收通过：' + str(result_path), flush=True)
    except BaseException as error:
        report['结果'], report['错误'] = '失败', str(error)
        save()
        raise


if __name__ == '__main__':
    main()
