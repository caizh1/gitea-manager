#!/usr/bin/env python3
"""独立 Gitea 单任务取消验收；协议替身只验证终止链路，不评价模型质量。"""
import argparse
import base64
import importlib.util
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('review_local', ROOT / 'scripts/review-local.py')
local = importlib.util.module_from_spec(spec); spec.loader.exec_module(local)


def wait_task(h, repository, number, predicate, seconds=90):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        rows = h.manager('GET', f'/ai-review/tasks?repository={repository}&number={number}')['items']
        if rows and predicate(rows[0]):
            return rows[0]
        if rows and rows[0]['status'] == 'failed':
            raise RuntimeError('任务在取消验收前失败：' + rows[0]['error'])
        time.sleep(.25)
    raise RuntimeError('等待验收任务状态超时')


def create_pr(h, repo):
    branch = 'cancel-' + str(time.time_ns())
    prefix = '/repos/' + repo['repo_name']
    h.gitea('POST', prefix+'/branches', {'new_branch_name': branch, 'old_branch_name': 'main'})
    code = '/* 独立取消验收固定源码。 */\nint value(int n) { return n + 1; }\n'
    h.gitea('POST', prefix+'/contents/cancel.c', {'branch': branch,
        'content': base64.b64encode(code.encode()).decode(), 'message': '增加单任务取消验收源码'})
    return h.gitea('POST', prefix+'/pulls', {'head': branch, 'base': 'main',
        'title': '单任务取消验收', 'body': '仅验证停止、状态及不发布结果，不用于模型质量评价。'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-ui', action='store_true', help='准备真实模型等待任务，供页面鼠标取消')
    args = parser.parse_args()
    h = local.Harness()
    if not h.values.get('REVIEW_PROJECT', '').startswith('gitea-review-cancel-'):
        raise RuntimeError('仅允许独立取消验收项目，禁止访问用户现有部署')
    h.login()
    configuration = h.manager('GET', '/ai-review/config')['config']
    if not configuration['enabled'] or configuration['model'] != 'review-fixture':
        h.configure_model('http://review-model-fixture:5053/v1', 'review-fixture', 'opencode_native', h.values['INTEGRATION_TOKEN'])
    h.request('POST', 'http://127.0.0.1:'+h.values['REVIEW_FIXTURE_PORT']+'/test/control',
              {'delay': 180}, {'Authorization': 'Bearer '+h.values['INTEGRATION_TOKEN']})
    repo = h.create_repo('cancel-acceptance')
    if args.prepare_ui:
        pr = create_pr(h, repo)
        job = wait_task(h, repo['id'], pr['number'], lambda row: row.get('model_calls', 0) > 0)
        data = {'说明': '真实页面取消专用任务，模型协议替身等待 180 秒。',
                '仓库': repo['id'], 'PR': pr['number'], '任务': job['id'], '会话': job['session']['id'],
                'Manager': h.manager_url+'/ai-reviews?task='+job['id']}
        (local.STATE/'ui-task.json').write_text(json.dumps(data, ensure_ascii=False, indent=2))
        print(json.dumps(data, ensure_ascii=False))
        return
    results = []
    for scenario in ('queued', 'waiting', 'disconnected', 'worker_restart'):
        if scenario == 'queued': local.compose('stop', 'review-worker')
        pr = create_pr(h, repo)
        job = wait_task(h, repo['id'], pr['number'],
                        lambda row: row['status'] == 'queued' if scenario == 'queued' else row.get('model_calls', 0) > 0)
        if scenario == 'disconnected': local.run(['docker', 'pause', h.values['REVIEW_PROJECT']+'-review-executor-1'])
        started = time.monotonic()
        accepted = h.manager('POST', '/ai-review/tasks/'+job['id']+'/cancel', {})['task']
        if scenario == 'worker_restart': local.compose('restart', 'review-worker')
        if scenario == 'disconnected':
            time.sleep(3)
            self_check = h.manager('GET', '/ai-review/tasks/'+job['id'])
            assert self_check['status'] == 'cancelling', '失联时错误显示取消成功'
            local.run(['docker', 'unpause', h.values['REVIEW_PROJECT']+'-review-executor-1'])
        terminal = wait_task(h, repo['id'], pr['number'], lambda row: row['status'] == 'cancelled', seconds=45)
        elapsed = round(time.monotonic()-started, 3)
        if scenario == 'waiting': assert elapsed < 10, '正常取消超过十秒'
        reviews = h.gitea('GET', '/repos/'+repo['repo_name']+f"/pulls/{pr['number']}/reviews")
        assert not reviews, '取消任务发布了正式 Review'
        if scenario == 'queued': local.compose('start', 'review-worker')
        h.manager('POST', '/ai-review/tasks/'+job['id']+'/cancel', {})
        # 周期扫描与相同版本通知均不能把取消任务复活。
        time.sleep(1)
        assert h.manager('GET', '/ai-review/tasks/'+job['id'])['status'] == 'cancelled'
        results.append({'场景': scenario, '首次状态': accepted['status'], '最终状态': terminal['status'],
                        '耗时秒': elapsed, '任务': job['id'], '正式审查数量': len(reviews)})
        print('已通过取消场景：'+scenario, flush=True)
    # 同版本人工重审与同 PR 新提交均创建新任务；再次取消后不发布正式意见。
    rerun = h.manager('POST', f"/ai-review/repositories/{repo['id']}/rerun", {'number': pr['number']})['job']
    manual = wait_task(h, repo['id'], pr['number'], lambda row: row['id'] == rerun and row.get('model_calls', 0) > 0)
    h.manager('POST', '/ai-review/tasks/'+manual['id']+'/cancel', {})
    wait_task(h, repo['id'], pr['number'], lambda row: row['status'] == 'cancelled')
    results.append({'场景': '人工重审', '任务': manual['id'], '最终状态': 'cancelled'})
    prefix = '/repos/'+repo['repo_name']
    branch = pr['head']['ref']
    content = h.gitea('GET', prefix+'/contents/cancel.c?ref='+branch)
    h.gitea('PUT', prefix+'/contents/cancel.c', {'branch': branch, 'sha': content['sha'],
        'content': base64.b64encode('/* 新提交取消验收。 */\nint value(int n) { return n + 2; }\n'.encode()).decode(),
        'message': '更新取消后的同一 PR 代码'})
    changed = wait_task(h, repo['id'], pr['number'], lambda row: row['id'] not in (job['id'], manual['id']) and row.get('model_calls', 0) > 0)
    h.manager('POST', '/ai-review/tasks/'+changed['id']+'/cancel', {})
    wait_task(h, repo['id'], pr['number'], lambda row: row['status'] == 'cancelled')
    assert not h.gitea('GET', prefix+f"/pulls/{pr['number']}/reviews")
    results.append({'场景': '新提交自动审查', '任务': changed['id'], '最终状态': 'cancelled'})
    path = local.STATE/'acceptance.json'
    path.write_text(json.dumps({'说明': '真实 Gitea 与交付候选镜像，协议替身仅验证停止链路。',
                               '结果': results}, ensure_ascii=False, indent=2)+'\n')
    print('取消验收证据：'+str(path))


if __name__ == '__main__':
    main()
