#!/usr/bin/env python3
"""在独立真实 Gitea 上验收进度；协议替身只验证状态链路，不代表模型质量。"""
import argparse
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('本机验收', ROOT/'scripts/review-local.py')
local = importlib.util.module_from_spec(spec); spec.loader.exec_module(local)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true', help='初始化独立测试环境并安装组件')
    parser.add_argument('--mode', default='normal', choices=['normal', 'invalid_json', '429', 'wrong_quote'])
    parser.add_argument('--delay', type=int, default=5)
    args = parser.parse_args()
    h = local.Harness()
    if 'progress' not in h.values.get('REVIEW_PROJECT', ''):
        raise RuntimeError('只允许名称含 progress 的独立环境，禁止操作用户手动测试环境')
    if args.prepare:
        h.initialize()
    else:
        h.login()
    repo = h.create_repo('progress-acceptance')
    config = h.manager('GET', '/ai-review/config')['config']
    if not config['enabled'] or config['model'] != 'review-fixture' or config['engine'] != 'new_api_http':
        h.configure_model('http://review-model-fixture:5053/v1', 'review-fixture', 'new_api_http', h.values['INTEGRATION_TOKEN'])
        config = h.manager('GET', '/ai-review/config')['config']
    config.pop('api_key_configured')
    if config['debounce_seconds'] != 20:
        config['debounce_seconds'] = 20; h.manager('PUT', '/ai-review/config', config)
    if args.prepare:
        raw = h.request('GET', h.manager_url+f"/api/ai-review/servers/{repo['server_id']}/progress-assets", opener=h.opener, raw=True)
        assets = local.STATE/'进度组件'; assets.mkdir(exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive: archive.extractall(assets)
        container = local.compose('ps', '-q', 'gitea').strip()
        for action in ('check', 'install', 'install'):
            local.run(['python3', str(assets/'install.py'), action, '--container', container], capture=False)
        print('独立 Gitea 组件安装和重复安装完成。', flush=True)
        return
    fixture = 'http://127.0.0.1:' + h.values.get('REVIEW_FIXTURE_PORT', '13053')
    h.request('POST', fixture+'/test/control', {'mode': args.mode, 'delay': args.delay}, {'Authorization':'Bearer '+h.values['INTEGRATION_TOKEN']})
    branch = 'progress-'+str(time.time_ns())
    name = repo['repo_name']; prefix = '/repos/'+name
    h.gitea('POST', prefix+'/branches', {'new_branch_name': branch, 'old_branch_name':'main'})
    code = '/* 独立进度协议验收，不是盲测代码。 */\n' + '\n'.join(
        f'int calculate_{i}(int value) {{\n    if (value < 0) return 0;\n    return value + {i}; /* REVIEW_TEST_TARGET */\n}}\n' for i in range(3))
    h.gitea('POST', prefix+'/contents/progress.c', {'branch':branch, 'content':base64.b64encode(code.encode()).decode(), 'message':'增加合成进度验收函数'})
    started = time.time()
    pr = h.gitea('POST', prefix+'/pulls', {'head':branch, 'base':'main', 'title':'审查进度验收 '+args.mode, 'body':'只验证进度、版本与结果展示，不用于模型质量评价。'})
    print('独立验收 PR：'+pr['html_url'], flush=True)
    observations = []; visible = None; task = None
    deadline = time.time()+240
    while time.time() < deadline:
        tasks = h.manager('GET', f"/ai-review/tasks?repository={repo['id']}")['items']
        task = next((j for j in tasks if j['number'] == pr['number']), None)
        rows = h.gitea('GET', prefix+f"/issues/{pr['number']}/comments", actor='developer')
        owned = [r for r in rows if r['user']['id']==repo['bot_id'] and r['body'].startswith('<!-- ai-review:summary:v1 -->')]
        if len(owned)>1: raise RuntimeError('发现重复总体状态评论')
        payload = None
        if owned:
            payload = json.loads(base64.urlsafe_b64decode(owned[0]['body'].split('<!-- ai-review:progress:v1:')[1].split(' -->')[0]))
            if visible is None: visible = time.time()-started
        key = (task['status'], task['progress']['stage'], task['progress']['units_completed'], payload['status'] if payload else None) if task else None
        if not observations or observations[-1]['状态'] != key:
            observations.append({'经过秒':round(time.time()-started,2), '状态':key, '任务': task['id'] if task else None})
            print('进度：'+json.dumps(observations[-1],ensure_ascii=False), flush=True)
        if task and payload and task['status'] in ('succeeded','failed') and payload['status'] == task['status']:
            break
        time.sleep(0.5)
    else: raise RuntimeError('等待真实 Gitea 进度回写超时')
    if visible is None or visible >= 10: raise RuntimeError('进度首次可见时间未达到 10 秒目标')
    if (args.mode in ('normal','wrong_quote')) != (task['status']=='succeeded'): raise RuntimeError('任务最终状态不符合故障注入预期')
    report = {'说明':'真实 Gitea、Manager、Worker、执行器和进度进程；模型为协议替身，不代表真实模型识别质量。',
              'PR':pr['html_url'], 'SHA':pr['head']['sha'], '任务':task['id'], '模式':args.mode,
              '首次状态可见秒':round(visible,3), '过程':observations, '最终载荷':payload, '总体评论数':len(owned)}
    path = local.STATE/('进度验收-'+args.mode+'-'+str(pr['number'])+'.json')
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('验收记录：'+str(path), flush=True)


if __name__ == '__main__': main()
