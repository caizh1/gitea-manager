#!/usr/bin/env python3
"""冻结四十个规则正反例，在既有独立验收环境执行有规则与无规则对照。"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]
# 参数、违反版本、符合版本、输入与期望结果由人工预先固定。
GROUPS = [
    ('温度单位', '传感器原值单位为十分之一摄氏度，输出摄氏度。', 'raw', 'raw', 'raw / 10', [250], 25),
    ('价格单位', '输入为整数分，输出为元。', 'cents', 'cents', 'cents / 100', [1230], 12.3),
    ('页码协议', '外部页码从一开始，内部索引从零开始。', 'page', 'page', 'page - 1', [3], 2),
    ('重试预算', '总尝试次数包含第一次，额外重试数等于总次数减一。', 'attempts', 'attempts', 'attempts - 1', [4], 3),
    ('订单状态', '完成态编码为 done，旧的 completed 编码已废弃。', '', "'completed'", "'done'", [], 'done'),
    ('缓存时间', '配置单位为秒，缓存协议需要毫秒。', 'seconds', 'seconds', 'seconds * 1000', [5], 5000),
    ('目录隔离', '缓存键必须包含租户 ID 和资源 ID，中间用冒号分隔。', 'tenant, resource', 'str(resource)', "str(tenant) + ':' + str(resource)", ['alpha', 7], 'alpha:7'),
    ('软删除过滤', '正常列表只能返回 deleted 为 False 的记录。', 'rows', 'rows', "[row for row in rows if not row['deleted']]", [[{'deleted': True}, {'deleted': False}]], [{'deleted': False}]),
    ('状态范围', '成功状态只包括 200 到 299，两端包含。', 'code', 'code <= 300', '200 <= code < 300', [300], False),
    ('空值语义', '只有 None 使用默认值，零是有效业务值。', 'value', 'value or 10', '10 if value is None else value', [0], 0),
    ('批量顺序', '协议要求保留请求的原始顺序。', 'items', 'sorted(items)', 'list(items)', [[3, 1, 2]], [3, 1, 2]),
    ('去重语义', '计费请求重复项仍逐项计费，不能去重。', 'items', 'len(set(items))', 'len(items)', [[1, 1, 2]], 3),
    ('审计脱敏', '审计摘要只返回 user，不能包含请求中的 secret。', 'request', 'dict(request)', "{'user': request['user']}", [{'user': 'a', 'secret': 'demo'}], {'user': 'a'}),
    ('金额舍入', '计费规则为银行家舍入，半值取最近偶数。', 'value', 'int(value + 0.5)', 'round(value)', [2.5], 2),
    ('字段兼容', '新协议字段名称固定为 total_count。', 'count', "{'count': count}", "{'total_count': count}", [3], {'total_count': 3}),
    ('幂等标识', '请求幂等键保持大小写，不能自动转换。', 'key', 'key.lower()', 'key', ['AbC'], 'AbC'),
    ('包含上界', '允许的最大值为十，十本身有效。', 'value', 'value < 10', 'value <= 10', [10], True),
    ('空输入', '空列表返回 None。', 'values', 'values[0]', 'values[0] if values else None', [[]], None),
    ('零除保护', '分母为零时返回 None。', 'numerator, denominator', 'numerator / denominator', 'numerator / denominator if denominator else None', [1, 0], None),
    ('记录存在', '缺失的键返回 None。', 'records, key', 'records[key]', 'records.get(key)', [{}, 'missing'], None),
]


def corpus():
    rows = []
    for number, (name, rule, args, bad, good, inputs, expected) in enumerate(GROUPS, 1):
        for defect, body in ((True, bad), (False, good)):
            ident = 'r' + hashlib.sha256((name + str(defect)).encode()).hexdigest()[:10]
            rows.append({'id': ident,
                         'group': number, '名称': name, '规则': '# 项目审查要求\n\n' + rule + '\n',
                         '存在缺陷': defect, '原代码': f'def convert({args}):\n    pass\n',
                         '代码': f'def convert({args}):\n    return {body}\n',
                         '输入': inputs, '预期': expected})
    return rows


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def run(output, rows, limit):
    if (output / 'runs.json').exists():
        raise RuntimeError('输出目录已有运行记录，请指定新的 --output 保留首次结果')
    # 复用专用本机环境，拒绝在没有既有隔离配置的情况下初始化新服务。
    state = Path(os.environ.get('REVIEW_STATE_DIR', ROOT / 'work/review-rules-environment'))
    if not (state / '.env').is_file():
        raise RuntimeError('请用 REVIEW_STATE_DIR 指定已初始化的独立验收环境')
    local = module('rules_local', 'review-local.py')
    real = module('rules_real', 'review-real-acceptance.py')
    harness = local.Harness()
    harness.login()
    config = harness.manager('GET', '/ai-review/config')['config']
    if config.get('engine') != 'codex_cli' or 'deepseek' not in config.get('model', '').lower():
        raise RuntimeError('验收环境需要配置实际部署的 DeepSeek 与 Codex 执行器')
    write(output / 'model.json', {k: config.get(k) for k in ('engine', 'model', 'revision', 'thinking_enabled', 'reasoning_effort')})
    run_id = str(time.time_ns())
    results = []
    for with_rules in (False, True):
        name = 'rules-acceptance-' + run_id + ('-rules' if with_rules else '-baseline')
        configured = harness.create_repo(name)
        folder = real.clone(harness, name)
        for row in rows[:limit]:
            base_branch = row['id'] + '-base'
            head_branch = row['id'] + '-head'
            real.git(harness, folder, 'checkout', '-b', base_branch, 'origin/main')
            (folder / 'sample.py').write_text(row['原代码'])
            if with_rules:
                (folder / '.gitea').mkdir(exist_ok=True)
                (folder / '.gitea/review-rules.md').write_text(row['规则'])
            real.git(harness, folder, 'add', '.')
            real.git(harness, folder, 'commit', '-m', '固定验收基线')
            base = real.git(harness, folder, 'rev-parse', 'HEAD').strip()
            real.git(harness, folder, 'push', 'origin', base_branch)
            real.git(harness, folder, 'checkout', '-b', head_branch)
            (folder / 'sample.py').write_text(row['代码'])
            real.git(harness, folder, 'add', 'sample.py')
            real.git(harness, folder, 'commit', '-m', '验收变更')
            head = real.git(harness, folder, 'rev-parse', 'HEAD').strip()
            real.git(harness, folder, 'push', 'origin', head_branch)
            pr = harness.gitea('POST', '/repos/' + configured['repo_name'] + '/pulls',
                               {'head': head_branch, 'base': base_branch, 'title': '规则验收 ' + row['id'], 'body': '独立合成验收样本'})
            record = {'样本': row['id'], '使用规则': with_rules, '仓库': configured['repo_name'], 'PR': pr['number'], 'base': base, 'head': head}
            results.append(record)
            write(output / 'runs.json', results)
            task, elapsed = real.wait_task(harness, configured['id'], pr['number'], head, timeout=3600)
            if (task.get('configuration') or {}).get('revision') != config['revision']:
                record.update(状态='配置变化', 任务=task['id'])
                write(output / 'runs.json', results)
                raise RuntimeError('验收期间模型配置发生变化，已停止混合配置对照')
            record.update(任务=task['id'], 状态=task['status'], 耗时秒=elapsed, 结果=task.get('result'),
                          Token=task.get('model_usage'), 规则=task.get('review_rules'),
                          人工核验=None, 人工基线秒=None, 人工复核秒=None)
            write(output / 'runs.json', results)
            print(row['id'], '有规则' if with_rules else '无规则', task['status'], flush=True)


def score(output):
    records = json.loads((output / 'runs.json').read_text())
    expected = {(row['id'], arm) for row in corpus() for arm in (False, True)}
    actual = {(row['样本'], row['使用规则']) for row in records}
    if actual != expected or len(records) != len(expected):
        raise RuntimeError('完整验收需要四十个样本的两组结果，共八十条记录')
    summaries = []
    for arm in (False, True):
        rows = [row for row in records if row['使用规则'] == arm]
        totals = dict.fromkeys(('正确意见数', '错误意见数', '漏报数', 'P0P1误报数'), 0)
        baseline = intervention = elapsed = tokens = 0
        for row in rows:
            verdict = row.get('人工核验')
            if row.get('状态') != 'succeeded' or not isinstance(verdict, dict):
                raise RuntimeError('存在未完成或未独立核验的样本，不能计算质量结论')
            for key in totals:
                count = verdict.get(key)
                if type(count) is not int or count < 0:
                    raise RuntimeError('人工核验计数必须为非负整数')
                totals[key] += count
            for key in ('人工基线秒', '人工复核秒'):
                if type(row.get(key)) not in (float, int) or row[key] < 0:
                    raise RuntimeError('缺少有效的人工耗时记录')
            baseline += row['人工基线秒']
            intervention += row['人工复核秒']
            elapsed += row['耗时秒']
            tokens += sum((row.get('Token') or {}).values())
        tp, fp, fn = (totals[key] for key in ('正确意见数', '错误意见数', '漏报数'))
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        savings = 1 - intervention / baseline if baseline else None
        passed = (precision is not None and precision >= .95 and recall is not None and recall >= .9
                  and totals['P0P1误报数'] == 0 and savings is not None and savings >= .8)
        summaries.append(dict(totals, 使用规则=arm, 准确率=precision, 召回率=recall,
                              人工节省=savings, 总耗时秒=elapsed, 总Token=tokens,
                              质量门槛='通过' if passed else '未达到'))
    write(output / 'quality.json', {'结果': summaries})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'work/review-rules-acceptance')
    parser.add_argument('--score', action='store_true', help='依据八十条结果中的独立人工核验与耗时计算指标')
    parser.add_argument('--run', action='store_true', help='使用既有独立环境执行两组真实模型对照')
    parser.add_argument('--limit', type=int, default=40, help='探索时可缩小样本数，完整验收必须使用 40')
    args = parser.parse_args()
    if not 1 <= args.limit <= 40:
        parser.error('--limit 必须为 1 至 40')
    args.output.mkdir(parents=True, exist_ok=True)
    rows = corpus()
    for row in rows:
        namespace = {}
        exec(compile(row['代码'], row['id'], 'exec'), namespace)
        try:
            matches = namespace['convert'](*row['输入']) == row['预期']
        except (ZeroDivisionError, IndexError, KeyError):
            matches = False
        assert matches is not row['存在缺陷'], row['id']
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
    write(args.output / 'cases.json', {'说明': '二十组正反例，共四十个固定样本。预期行为只用于独立验收。',
                                      'SHA256': hashlib.sha256(payload).hexdigest(), '样本': rows})
    write(args.output / 'status.json', {'工程样本': '四十个输入与期望结果已校验', '实际模型': '未验证', '人工节省': '未验证'})
    if args.run:
        run(args.output, rows, args.limit)
    if args.score:
        score(args.output)
    print('固定样本已生成', args.output)


if __name__ == '__main__':
    main()
