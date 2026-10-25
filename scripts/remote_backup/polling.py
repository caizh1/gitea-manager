"""长任务观察会话失效时只重新认证和读取，绝不重复提交业务任务。"""
import time

from .errors import CheckFailure
from .host import write_json


def task_rows(lab, kind, identifier, refresh_used=False):
    if kind not in ('backup', 'restore') or type(identifier) is not int or identifier <= 0:
        raise CheckFailure('轮询任务类型或编号无效')
    path = '/backups' if kind == 'backup' else '/restore-tasks'
    response = lab.manager_api('GET', path, expected=None)
    if response.status_code == 401 and not refresh_used:
        evidence = lab.root / ('poll-authentication-%s-%s-%s.json' % (kind, identifier, time.time_ns()))
        if evidence.exists():
            raise CheckFailure('认证续读证据已存在，拒绝覆盖')
        record = {'说明': '只允许一次重新登录并读取原任务列表；不重放备份、恢复或其他业务写入，不重置原观察截止时间',
                  '任务类型': kind, '任务ID': identifier, '首次HTTP状态': 401, '状态': '准备重新认证'}
        write_json(evidence, record)
        try:
            lab.login()
            refresh_used = True
            response = lab.manager_api('GET', path, expected=None)
        except Exception as exc:
            record.update({'状态': '认证续读失败', '异常类型': type(exc).__name__})
            write_json(evidence, record)
            raise
        record.update({'状态': '续读成功' if response.status_code == 200 else '续读拒绝',
                       '续读HTTP状态': response.status_code})
        write_json(evidence, record)
    if response.status_code != 200:
        raise CheckFailure('Manager GET %s 返回 %s：%s' % (
            path, response.status_code, lab.scrub(response.text[:800])))
    rows = response.json()
    if not isinstance(rows, list):
        raise CheckFailure('Manager 任务列表格式无效，不能推断任务结果')
    return rows, refresh_used
