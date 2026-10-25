"""从任务与采样原始证据计算耗时；不把采样值当作生产 SLA。"""
import json
import math
import re
from datetime import datetime, timezone
import requests


def probe_service(session, url, token):
    """认证失败与响应格式异常是未知状态，不能冒充服务停机。仅保存分类，不记录凭据或正文。"""
    if not token:
        return {'可用': None, '分类': '缺少认证身份'}
    try:
        response = session.get(url + '/api/v1/version', headers={'Authorization': 'token ' + token},
                               timeout=0.5, allow_redirects=False)
    except requests.RequestException as exc:
        return {'可用': False, '分类': '连接失败', '异常类型': type(exc).__name__}
    result = {'可用': None, 'HTTP状态': response.status_code, '分类': '响应未确认'}
    if response.status_code >= 500:
        result.update(可用=False, 分类='服务错误')
    elif response.status_code == 200:
        try:
            if isinstance(response.json().get('version'), str) and response.json()['version']:
                result.update(可用=True, 分类='认证版本接口正常')
        except (ValueError, AttributeError):
            pass
    elif response.status_code in (401, 403):
        result['分类'] = '认证被拒绝，服务停机未确认'
    return result


def elapsed_seconds(start, end):
    """Manager 的无时区 ISO 时间按其 UTC 约定解释；缺失或倒退不能虚构完整耗时。"""
    if not start or not end:
        return None
    try:
        parsed = [datetime.fromisoformat(value.replace('Z', '+00:00')) for value in (start, end)]
        parsed = [value if value.tzinfo else value.replace(tzinfo=timezone.utc) for value in parsed]
        elapsed = (parsed[1] - parsed[0]).total_seconds()
        return round(elapsed, 3) if elapsed >= 0 else None
    except (ValueError, TypeError):
        return None


def continuity_issue(previous, current):
    """稳定性写入心跳不能把宿主睡眠、时钟跳变或长时间停采算成连续运行。"""
    if previous is None:
        return None
    wall = current['墙钟秒'] - previous['墙钟秒']
    monotonic = current['单调秒'] - previous['单调秒']
    if wall < 0 or monotonic < 0 or abs(wall - monotonic) > 60 or max(wall, monotonic) > 120:
        return '写入心跳不连续：墙钟间隔 %.3f 秒，单调间隔 %.3f 秒；保留现场，不能宣称连续二十四小时通过' % (wall, monotonic)
    return None


def byte_count(value):
    """解析 Docker stats 的十进制与二进制单位，未知格式不当成零。"""
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*([kKMGT]?i?B)', value.strip())
    if not match:
        raise ValueError('资源采样单位不受支持')
    number, unit = match.groups()
    exponent = {'B': 0, 'kB': 1, 'KB': 1, 'MB': 2, 'GB': 3, 'TB': 4,
                'KiB': 1, 'MiB': 2, 'GiB': 3, 'TiB': 4}[unit]
    return int(float(number) * (1024 if 'i' in unit else 1000) ** exponent)


def collect(root):
    result = {'说明': '本机隔离环境实测；认证探针按相邻采样左端状态估计；旧匿名探针与未知响应不推断停机',
              '任务': [], '可用性': [], '阶段耗时': []}
    for path in sorted((root / 'phase-runs').glob('*.json')):
        phase = json.loads(path.read_text())
        if not phase.get('结束时间UTC'):
            continue
        wall = (datetime.fromisoformat(phase['结束时间UTC']) - datetime.fromisoformat(phase['开始时间UTC'])).total_seconds()
        measured = phase.get('耗时秒', phase.get('单调耗时秒'))
        if type(measured) not in (int, float) or not math.isfinite(measured) or measured < 0:
            raise ValueError('阶段缺少有效单调耗时，不能用墙钟或零值替代')
        if '耗时秒' in phase and '单调耗时秒' in phase and phase['耗时秒'] != phase['单调耗时秒']:
            raise ValueError('同一阶段的两个单调耗时字段冲突，不能选择较好结果')
        result['阶段耗时'].append({'文件': path.name, '阶段': phase['阶段'], '状态': phase['状态'],
            '单调计时秒': measured, '墙钟秒': round(wall, 3), '时钟差额秒': round(wall - measured, 3),
            '连续性能测量可用': abs(wall - measured) <= 60})
    for path in sorted((root / 'tasks').glob('*.json')):
        task = json.loads(path.read_text())
        total = elapsed_seconds(task.get('started_at'), task.get('completed_at'))
        result['任务'].append({'任务': path.stem, '状态': task.get('status'),
            '总耗时秒': total, '轮询观察秒': task.get('观察耗时秒', task.get('总耗时秒')),
            '计时来源': 'Manager 任务持久化起止 UTC' if total is not None else '缺少完整起止时间，不推断任务总耗时',
            '归档字节': task.get('file_size'), '阶段': task.get('阶段记录', [])})
    for path in sorted((root / 'availability').glob('*.json')):
        record = json.loads(path.read_text())
        rows = record['样本']
        interval = [max(0, right['秒'] - left['秒']) for left, right in zip(rows, rows[1:])]
        row = {'文件': path.name, '操作': record['操作'], '采样数': len(rows),
               '最大采样间隔秒': round(max(interval, default=0), 3)}
        if rows and all('墙钟秒' in sample for sample in rows):
            wall = [right['墙钟秒'] - left['墙钟秒'] for left, right in zip(rows, rows[1:])]
            row['最大墙钟采样间隔秒'] = round(max(wall, default=0), 3)
            row['存在睡眠或时钟空档'] = any(abs(a - b) > 60 for a, b in zip(wall, interval))
        else:
            row['墙钟连续性'] = '旧采样未记录墙钟，不能据此推断宿主睡眠期间的可用性'
        for node in ('a', 'b'):
            valid = record.get('探针版本') == 2
            row[node + '估算不可用秒'] = (round(sum(delta for delta, sample in zip(interval, rows)
                if sample[node] is False), 3) if valid else None)
            row[node + '未确认样本数'] = sum(sample[node] is None for sample in rows) if valid else len(rows)
        row['Gitea测量有效性'] = ('认证版本探针；未知样本不计作停机或健康' if record.get('探针版本') == 2
            else '无效：旧匿名版本接口可能返回 403；原始计数保留，不能估算 Gitea 停机')
        row['Manager会话失败样本数'] = sum(not sample['Manager会话可用'] for sample in rows)
        row['Manager写入失败样本数'] = sum(sample.get('Manager写入可用') is False for sample in rows)
        row['Manager最长响应秒'] = max((sample['Manager响应秒'] for sample in rows), default=0)
        result['可用性'].append(row)
    resource = root / 'resources.jsonl'
    samples = [json.loads(line) for line in resource.read_text().splitlines()] if resource.exists() else []
    result['资源采样数'] = len(samples)
    result['宿主最低可用字节'] = min((row['宿主可用字节'] for row in samples), default=None)
    totals = []
    peaks = {}
    for sample in samples:
        memory = []
        for container in sample.get('容器', []):
            try:
                count = byte_count(container['MemUsage'].split('/')[0])
            except (KeyError, ValueError):
                continue
            memory.append(count)
            name = container['Name']
            peaks[name] = max(peaks.get(name, 0), count)
        if memory:
            totals.append(sum(memory))
    result['本轮容器内存采样峰值字节'] = max(totals, default=None)
    result['各容器内存采样峰值字节'] = peaks
    result['磁盘峰值边界'] = '宿主可用空间包括其他任务影响，不能据此声称测得本轮独占磁盘峰值'
    result['HTTP下载'] = [json.loads(path.read_text()) for path in sorted((root / 'backups').glob('download-*.json'))]
    return result
