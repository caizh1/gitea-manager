#!/usr/bin/env python3
"""原版 Gitea、真实 Git 与真实模型的直接行内发布验收；仅使用本文合成代码。"""
import argparse
import importlib.util
import io
import json
from pathlib import Path
import runpy
import subprocess
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
real = runpy.run_path(str(ROOT/'scripts/review-real-acceptance.py'))
local = real['local']


def install_assets(h, repo):
    raw = h.request('GET', h.manager_url+f"/api/ai-review/servers/{repo['server_id']}/progress-assets", opener=h.opener, raw=True)
    target = local.STATE/'进度组件'; target.mkdir(exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        archive.extractall(target)
    local.run(['python3', str(target/'install.py'), 'install', '--container', local.compose('ps', '-q', 'gitea').strip()], capture=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-container', help='可选：从指定本机 Worker 的加密配置转入临时模型连接，仅在内存中传递密钥')
    args = parser.parse_args()
    h = local.Harness(); h.login()
    if 'direct' not in h.values.get('REVIEW_PROJECT', ''):
        raise RuntimeError('只允许直接发布的独立验收环境')
    version = h.gitea('GET', '/version')['version']
    if version != '1.26.4':
        raise RuntimeError('本验收必须使用官方原版 Gitea 1.26.4')
    if args.source_container:
        code = "from ai_review.store import Store; import json; s=Store(); c=s.config(); c['api_key']=s.unseal(c['api_key']); print(json.dumps({k:c[k] for k in ('api_base_url','model','engine','api_key')}))"
        cfg = json.loads(subprocess.check_output(['docker', 'exec', args.source_container, 'python', '-c', code], text=True))
        h.configure_model(cfg['api_base_url'], cfg['model'], cfg['engine'], cfg.pop('api_key'))
    cfg = h.manager('GET', '/ai-review/config')['config']
    if not cfg['enabled'] or cfg['model'] == 'review-fixture':
        raise RuntimeError('必须先配置真实模型，不能使用协议替身完成本项验收')
    repo = h.create_repo('direct-real-model')
    install_assets(h, repo)
    folder = real['clone'](h, 'direct-real-model'); git = real['git']
    branch = 'direct-real-'+str(time.time_ns())
    git(h, folder, 'switch', 'main'); git(h, folder, 'switch', '-c', branch)
    source = '#include <stddef.h>\n\nint sample_at(const int *values, size_t count, size_t index)\n{\n    if (values == NULL || index > count) return 0;\n    return values[index];\n}\n'
    (folder/'sample_at.c').write_text(source)
    git(h, folder, 'add', 'sample_at.c'); git(h, folder, 'commit', '-m', '增加独立合成读取函数')
    git(h, folder, 'push', 'origin', 'HEAD')
    sha = git(h, folder, 'rev-parse', 'HEAD').strip()
    prefix = '/repos/'+repo['repo_name']
    pr = h.gitea('POST', prefix+'/pulls', {'head': branch, 'base': 'main', 'title': '原版 Gitea 直接发布真实模型验收', 'body': '仅使用新生成的合成代码，不包含公司代码或用户盲测样本。'})
    url = h.gitea_url+'/'+repo['repo_name']+'/pulls/'+str(pr['number'])
    print('真实模型 PR：'+url, flush=True)
    task, elapsed = real['wait_task'](h, repo['id'], pr['number'], sha)
    reviews = h.gitea('GET', prefix+f"/pulls/{pr['number']}/reviews", actor='developer')
    comments = [c for r in reviews for c in h.gitea('GET', prefix+f"/pulls/{pr['number']}/reviews/{r['id']}/comments", actor='developer')]
    valid = [c for c in comments if c['path']=='sample_at.c' and c['position'] in (5,6) and not c['original_position']]
    passed = (task['status']=='succeeded' and len(reviews)==1 and reviews[0]['state']=='COMMENT'
              and reviews[0]['commit_id']==sha and len(valid)==len(comments)>0)
    report = {'说明':'真实 Git 提交、Gitea Webhook、独立 Worker、真实模型与正式行内回写；页面检查另行记录。',
              '通过':passed, 'Gitea版本':version, '模型':cfg['model'], '引擎':cfg['engine'], 'PR':url, '任务':task['id'], 'SHA':sha,
              '状态':task['status'], '耗时秒':round(elapsed,2), '错误':task['error'], '预期文件':'sample_at.c', '预期位置':'新侧第 5 行条件或第 6 行访问',
              '实际位置':[{'文件':c['path'],'新侧行号':c['position'],'旧侧行号':c['original_position'],'代码片段':c['diff_hunk']} for c in comments],
              'Review编号':[r['id'] for r in reviews], '限制':'发布期间版本稳定；不代表内网模型质量或并发 Push 防错位验收。'}
    (local.STATE/'真实模型直接发布验收.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('真实模型直接发布：'+('通过' if passed else '失败，已保存首次证据'), flush=True)
    if not passed:
        raise RuntimeError('真实模型直接发布未通过，不能标记完成')


if __name__ == '__main__':
    main()
