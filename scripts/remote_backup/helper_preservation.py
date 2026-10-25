"""保留已有停止助手的首败现场，严格拒绝新残留和旧助手变化。"""
import json
import re

from .errors import CheckFailure


def snapshot(lab, node):
    identifiers = lab.command(node, ['docker', 'ps', '-aq', '--no-trunc',
        '--filter', 'label=gitea.manager.operation'])[1].decode().splitlines()
    result = {}
    for identifier in identifiers:
        if not re.fullmatch(r'[a-f0-9]{64}', identifier) or identifier in result:
            raise CheckFailure('助手完整身份无效或重复，不能建立保留基线')
        state = json.loads(lab.command(node, ['docker', 'inspect', '-f', '{{json .State}}', identifier])[1])
        result[identifier] = {key: state[key] for key in (
            'Status', 'Running', 'Paused', 'Restarting', 'ExitCode', 'StartedAt', 'FinishedAt')}
    return result


def compare(before, after):
    """旧停止现场不可删除或重启，任何新增助手无论运行或停止都算残留。"""
    if any(row['Status'] != 'exited' or row['Running'] or row['Paused'] or row['Restarting']
           for row in before.values()):
        raise CheckFailure('已有活动助手，不能开始长期稳定性写入')
    if before != after:
        raise CheckFailure('存在新助手残留，或原停止助手被删除、重启或改变状态')
